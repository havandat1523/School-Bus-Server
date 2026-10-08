import os
import json
import time
import base64
import cv2
import numpy as np
import logging
from pydantic import BaseModel
from fastapi import FastAPI, Depends, Request, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from database import get_db, engine, Base
from models import Vehicle, Driver, Attendant, Student, TelemetryLog, EventLog, Manager, TripSession, RouteStop, ManualAttendanceLog, EmergencyEvent, StudentAbsence
from mqtt_listener import ServerMQTTListener

# Configure logging to write to app.log and console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "app.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

class VerifyBody(BaseModel):
    driver_id: str
    image: str

class ParentLoginBody(BaseModel):
    phone: str
    password: str

# Initialize DB tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="SchoolBus Monitoring Dashboard")

# Allow Flutter Web (Chrome) and any local dev origin to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create templates & static directory stubs dynamically
current_dir = os.path.dirname(__file__)
templates_dir = os.path.join(current_dir, "templates")
static_dir = os.path.join(current_dir, "static")
os.makedirs(templates_dir, exist_ok=True)
os.makedirs(static_dir, exist_ok=True)

app.mount("/static", StaticFiles(directory=static_dir), name="static")
app.mount("/image", StaticFiles(directory=os.path.join(current_dir, "image")), name="image")
templates = Jinja2Templates(directory=templates_dir)

# Start MQTT Background listener on startup
mqtt_listener = None

@app.on_event("startup")
def startup_event():
    global mqtt_listener
    mqtt_listener = ServerMQTTListener()
    mqtt_listener.start()
    print("[Server App] Background MQTT listener thread spawned.")

@app.on_event("shutdown")
def shutdown_event():
    global mqtt_listener
    if mqtt_listener:
        mqtt_listener.running = False
        print("[Server App] Background MQTT listener thread stopped.")

# --- WEB USER INTERFACES ---

@app.get("/login", response_class=HTMLResponse)
def get_login(request: Request):
    if request.cookies.get("manager_session") == "manager1":
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": ""})

@app.post("/login")
def post_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    manager = db.query(Manager).filter(
        Manager.username == username,
        Manager.password == password
    ).first()
    
    if not manager:
        return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password!"})
        
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie(key="manager_session", value=username, max_age=3600)
    return response

@app.get("/logout")
def get_logout(request: Request):
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key="manager_session")
    return response

def format_event_log(type_id: int, details_json, vehicle_id: str) -> str:
    """
    Parses EventLog entry into human-readable English description for Admin Dashboard
    """
    details = {}
    if details_json:
        try:
            details = json.loads(details_json) if isinstance(details_json, str) else details_json
        except Exception:
            pass

    if type_id == 1:
        return f"👨‍✈️ Driver Login Request (Face Verification)"
    elif type_id == 2:
        name = details.get("full_name", "Driver")
        return f"✅ Driver [{name}] Logged In Successfully"
    elif type_id == 3:
        return f"🚪 Driver Logout Request"
    elif type_id == 4:
        return f"✅ Driver Logged Out Successfully"
    elif type_id == 5:
        count = details.get("count", 1)
        return f"⚠️ Warning: Driver Face Mismatch! (Attempt {count}/4)"
    elif type_id == 6:
        rfid = details.get("rfid_code", "")
        return f"🪪 Attendant RFID Scan [UID: {rfid}]"
    elif type_id == 7:
        name = details.get("full_name", "Attendant")
        return f"✅ Attendant [{name}] Logged In Successfully"
    elif type_id == 8:
        return f"🚪 Attendant Logout Request"
    elif type_id == 9:
        return f"✅ Attendant Logged Out Successfully"
    elif type_id == 10:
        return f"⚠️ Warning: Attendant Absent From Seat > 3 Mins!"
    elif type_id == 11:
        rfid = details.get("rfid_code", "")
        seat = details.get("seat_number", 0)
        seat_str = f" (Seat {seat})" if seat > 0 else ""
        return f"🎒 Student RFID Scan [{rfid}]{seat_str}"
    elif type_id == 12:
        return f"🎒 Student Boarding / Drop-off Event"
    elif type_id == 13:
        students = details.get("students_onboard", 0)
        return f"💺 Seat Map Updated ({students} Students Onboard)"
    elif type_id == 14:
        speed = details.get("speed_kmh", 0.0)
        return f"📍 GPS Telemetry: Speed {speed:.1f} km/h"
    elif type_id == 15:
        return f"🚨 EMERGENCY SOS ALERT ON BUS {vehicle_id}!"
    elif type_id == 16:
        return f"🚨 ABSENT CHILD RISK: Students Onboard Without Crew!"
    elif type_id == 20:
        action = details.get("action", "")
        return f"⚙️ Server Command Executed: {action}"
    elif type_id == 21:
        return f"🔄 Vehicle Connected & Requested Session Approval"
    elif type_id == 22:
        st = details.get("session_state", 0)
        st_desc = {0: "NEW", 1: "PENDING_APPROVAL", 2: "APPROVED", 3: "REJECTED"}.get(st, "UNKNOWN")
        return f"🔄 Server Session Response: {st_desc}"
    else:
        return f"📋 System Event Type {type_id} (Bus {vehicle_id})"

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        return RedirectResponse(url="/login", status_code=303)
    vehicles = db.query(Vehicle).all()
    # Get recent logs
    recent_logs = db.query(EventLog).order_by(EventLog.timestamp.desc()).limit(20).all()
    
    formatted_logs = []
    for log in recent_logs:
        try:
            details = json.loads(log.details)
        except:
            details = {}
        formatted_logs.append({
            "vehicle_id": log.vehicle_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(log.timestamp)),
            "type": log.type,
            "details": details,
            "description": format_event_log(log.type, log.details, log.vehicle_id)
        })
        
    return templates.TemplateResponse(request, "dashboard.html", {
        "vehicles": vehicles, 
        "logs": formatted_logs
    })

@app.get("/drivers", response_class=HTMLResponse)
def view_drivers(request: Request, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        return RedirectResponse(url="/login", status_code=303)
    drivers = db.query(Driver).all()
    active_driver_ids = {v.active_driver_id for v in db.query(Vehicle).filter(Vehicle.active_driver_id != None).all()}
    
    drivers_data = []
    for d in drivers:
        duty = "ON DUTY" if d.driver_id in active_driver_ids else "OFF DUTY"
        drivers_data.append({
            "driver_id": d.driver_id,
            "full_name": d.full_name,
            "citizen_id": d.citizen_id,
            "license_class": d.license_class,
            "license_number": d.license_number,
            "phone": d.phone or "---",
            "email": d.email or "---",
            "status": d.status,
            "duty": duty
        })
        
    return templates.TemplateResponse(request, "drivers.html", {
        "drivers": drivers_data
    })

@app.get("/students", response_class=HTMLResponse)
def view_students(request: Request, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        return RedirectResponse(url="/login", status_code=303)
    students = db.query(Student).all()
    vehicles = db.query(Vehicle).all()
    today_date = time.strftime("%Y-%m-%d")
    return templates.TemplateResponse(request, "students.html", {
        "students": students,
        "vehicles": vehicles,
        "today_date": today_date
    })

@app.get("/attendants", response_class=HTMLResponse)
def view_attendants(request: Request, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        return RedirectResponse(url="/login", status_code=303)
    attendants = db.query(Attendant).all()
    return templates.TemplateResponse(request, "attendants.html", {
        "attendants": attendants
    })

# --- FORM SUBMISSIONS ---

@app.post("/drivers/add")
def add_driver(
    request: Request,
    driver_id: str = Form(...),
    full_name: str = Form(...),
    citizen_id: str = Form(...),
    license_class: str = Form(...),
    license_number: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    did = driver_id.strip()
    cccd = citizen_id.strip()
    
    # Check duplicate Driver ID
    existing_id = db.query(Driver).filter(Driver.driver_id == did).first()
    if existing_id:
        logging.warning(f"[Admin Verification] Attempt to register duplicate Driver ID: {did}")
        raise HTTPException(status_code=400, detail="Driver ID is already registered!")
        
    # Check duplicate Citizen ID (CCCD)
    existing_cccd = db.query(Driver).filter(Driver.citizen_id == cccd).first()
    if existing_cccd:
        logging.warning(f"[Admin Verification] Attempt to register duplicate Citizen ID (CCCD): {cccd}")
        raise HTTPException(status_code=400, detail="Citizen ID (CCCD) is already registered to another driver!")

    driver = Driver(
        driver_id=did,
        full_name=full_name.strip(),
        citizen_id=cccd,
        license_class=license_class.strip(),
        license_number=license_number.strip(),
        phone=phone.strip(),
        email=email.strip(),
        status="pending",
        face_vector_json=None
    )
    db.add(driver)
    db.commit()
    logging.info(f"[Admin Action] Registered new pending driver profile: {did} - {full_name.strip()}")
    return RedirectResponse(url="/drivers", status_code=303)

@app.post("/drivers/{driver_id}/delete")
def delete_driver(request: Request, driver_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
    if driver:
        name = driver.full_name
        # Clear active_driver_id on any vehicle that references this deleted driver
        db.query(Vehicle).filter(Vehicle.active_driver_id == driver_id).update({"active_driver_id": None})
        db.delete(driver)
        db.commit()
        logging.info(f"[Admin Action] Deleted driver profile and reset vehicle active driver: {driver_id} - {name}")
    return RedirectResponse(url="/drivers", status_code=303)

@app.post("/drivers/{driver_id}/suspend")
def suspend_driver(request: Request, driver_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
    if driver:
        driver.status = "suspended"
        db.commit()
        logging.info(f"[Admin Action] Suspended driver profile: {driver_id} - {driver.full_name}")
    return RedirectResponse(url="/drivers", status_code=303)

@app.post("/drivers/{driver_id}/activate")
def activate_driver(request: Request, driver_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
    if driver:
        driver.status = "active"
        db.commit()
        logging.info(f"[Admin Action] Activated driver profile: {driver_id} - {driver.full_name}")
    return RedirectResponse(url="/drivers", status_code=303)

@app.post("/drivers/{driver_id}/edit")
def edit_driver(
    request: Request,
    driver_id: str,
    full_name: str = Form(...),
    citizen_id: str = Form(""),
    license_class: str = Form(""),
    license_number: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
    if not driver:
        raise HTTPException(status_code=404, detail="Driver not found")
    driver.full_name = full_name.strip()
    driver.citizen_id = citizen_id.strip()
    driver.license_class = license_class.strip()
    driver.license_number = license_number.strip()
    driver.phone = phone.strip()
    driver.email = email.strip()
    db.commit()
    logging.info(f"[Admin Action] Updated driver profile: {driver_id} - {driver.full_name}")
    return RedirectResponse(url="/drivers", status_code=303)

@app.post("/api/drivers/verify")
def verify_driver_face(body: VerifyBody, db: Session = Depends(get_db)):
    driver_id = body.driver_id
    image_base64 = body.image
    
    try:
        import face_recognition
        FACE_AVAILABLE = True
    except ImportError:
        FACE_AVAILABLE = False
        
    try:
        header, encoded = image_base64.split(",", 1) if "," in image_base64 else ("", image_base64)
        img_data = base64.b64decode(encoded)
        
        face_vector = None
        if FACE_AVAILABLE:
            np_arr = np.frombuffer(img_data, dtype=np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is not None:
                rgb_img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
                face_locations = face_recognition.face_locations(rgb_img)
                if face_locations:
                    encodings = face_recognition.face_encodings(rgb_img, face_locations)
                    if encodings:
                        face_vector = encodings[0].tolist()
                        
        if face_vector is None:
            # Fallback to OpenCV Haar Cascade face detector if face_recognition library is missing or didn't detect
            try:
                np_arr = np.frombuffer(img_data, dtype=np.uint8)
                img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                if img is not None:
                    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                    cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
                    face_cascade = cv2.CascadeClassifier(cascade_path)
                    faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
                    
                    if len(faces) > 0:
                        (x, y, w, h) = faces[0]
                        face_roi = cv2.resize(gray[y:y+h, x:x+w], (16, 8))
                        # Generate normalized 128-D vector from cropped face ROI
                        face_vector = [round(float(val), 4) for val in (face_roi.flatten() / 255.0 * 2.0 - 1.0)]
                        logging.info(f"[Biometrics] OpenCV Haar Cascade detected face for driver {driver_id}!")
            except Exception as e:
                logging.error(f"[Biometrics Error] OpenCV Haar Cascade failed: {e}")

        if face_vector is None:
            logging.warning(f"[Biometrics] No face detected in uploaded image for driver {driver_id}")
            raise HTTPException(
                status_code=400, 
                detail="No face detected in image! Please face the webcam directly."
            )
            
        # Biometric similarity check against all other registered driver face vectors
        active_drivers = db.query(Driver).filter(Driver.face_vector_json != None, Driver.driver_id != driver_id).all()
        for active_drv in active_drivers:
            try:
                other_vector = json.loads(active_drv.face_vector_json)
                if len(other_vector) == 128:
                    dist = np.linalg.norm(np.array(face_vector) - np.array(other_vector))
                    if dist < 0.6:
                        logging.warning(f"[Biometrics Failed] Duplicate face detected! Attempts to verify {driver_id} matches existing driver: {active_drv.full_name} ({active_drv.driver_id})")
                        raise HTTPException(
                            status_code=400,
                            detail=f"Biometric Match Found! This face is already registered to driver: {active_drv.full_name} ({active_drv.driver_id})"
                        )
            except HTTPException as hex:
                raise hex
            except:
                continue
            
        driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
        if not driver:
            raise HTTPException(status_code=404, detail="Driver not found")
            
        driver.face_vector_json = json.dumps(face_vector)
        driver.status = "active"
        db.commit()
        logging.info(f"[Biometrics Success] Successfully verified and enrolled face biometric for driver: {driver_id} - {driver.full_name}")
        return {"result": 1, "driver_id": driver_id}
    except Exception as e:
        if isinstance(e, HTTPException):
            raise e
        logging.error(f"[Biometrics Error] Face verification crashed for driver {driver_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/students/add")
def add_student(
    request: Request,
    rfid_code: str = Form(...),
    full_name: str = Form(...),
    parent_phone: str = Form(...),
    address: str = Form(...),
    student_class: str = Form(...),
    lat: float = Form(20.9806),
    lon: float = Form(105.7958),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    rfid = rfid_code.strip()
    
    # Check duplicate RFID in students
    existing_stud = db.query(Student).filter(Student.rfid_code == rfid).first()
    if existing_stud:
        logging.warning(f"[Admin Verification] Attempt to register duplicate student RFID: {rfid}")
        raise HTTPException(status_code=400, detail="RFID Card UID is already registered to another student!")
        
    # Check duplicate RFID in attendants
    existing_att = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
    if existing_att:
        logging.warning(f"[Admin Verification] Attempt to register student RFID already assigned to attendant: {rfid}")
        raise HTTPException(status_code=400, detail="RFID Card UID is already registered to an attendant!")

    student = Student(
        rfid_code=rfid,
        full_name=full_name.strip(),
        parent_phone=parent_phone.strip(),
        address=address.strip(),
        student_class=student_class.strip(),
        status="không ở trên xe",
        current_seat=0,
        lat=lat,
        lon=lon
    )
    db.add(student)
    db.commit()
    db.refresh(student)

    # Sync RouteStop
    max_order = db.query(RouteStop).filter(RouteStop.route_id == 1).count()
    route_stop = RouteStop(
        route_id=1,
        student_id=student.student_id,
        stop_order=max_order + 1,
        address=student.address,
        lat=lat,
        lon=lon,
        geometry="[]",
        distance_m=0.0,
        duration_s=0.0
    )
    db.add(route_stop)
    db.commit()

    logging.info(f"[Admin Action] Registered new student: {rfid} - {full_name.strip()} at ({lat}, {lon})")
    return RedirectResponse(url="/students", status_code=303)

@app.post("/students/{student_id}/delete")
def delete_student(request: Request, student_id: int, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    student = db.query(Student).filter(Student.student_id == student_id).first()
    if student:
        name = student.full_name
        # Also clean up route stop if any
        db.query(RouteStop).filter(RouteStop.student_id == student_id).delete()
        db.delete(student)
        db.commit()
        logging.info(f"[Admin Action] Deleted student: HS{student_id:04d} - {name}")
    return RedirectResponse(url="/students", status_code=303)

@app.post("/students/{student_id}/suspend")
def suspend_student(request: Request, student_id: int, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    student = db.query(Student).filter(Student.student_id == student_id).first()
    if student:
        student.status = "suspended"
        db.commit()
        logging.info(f"[Admin Action] Suspended student: HS{student_id:04d} - {student.full_name}")
    return RedirectResponse(url="/students", status_code=303)

@app.post("/students/{student_id}/activate")
def activate_student(request: Request, student_id: int, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    student = db.query(Student).filter(Student.student_id == student_id).first()
    if student:
        student.status = "không ở trên xe"
        db.commit()
        logging.info(f"[Admin Action] Activated student: HS{student_id:04d} - {student.full_name}")
    return RedirectResponse(url="/students", status_code=303)

@app.post("/students/{student_id}/edit")
def edit_student(
    request: Request,
    student_id: int,
    rfid_code: str = Form(...),
    full_name: str = Form(...),
    parent_phone: str = Form(...),
    address: str = Form(...),
    student_class: str = Form(...),
    lat: float = Form(20.9806),
    lon: float = Form(105.7958),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    student = db.query(Student).filter(Student.student_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    rfid = rfid_code.strip()
    if rfid != student.rfid_code:
        # Check duplicate RFID in students
        existing_stud = db.query(Student).filter(Student.rfid_code == rfid, Student.student_id != student_id).first()
        if existing_stud:
            raise HTTPException(status_code=400, detail="RFID Card UID is already registered to another student!")
        # Check duplicate RFID in attendants
        existing_att = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
        if existing_att:
            raise HTTPException(status_code=400, detail="RFID Card UID is already registered to an attendant!")
        student.rfid_code = rfid

    student.full_name = full_name.strip()
    student.parent_phone = parent_phone.strip()
    student.address = address.strip()
    student.student_class = student_class.strip()
    student.lat = lat
    student.lon = lon
    
    # Sync RouteStop if exists or create
    stop = db.query(RouteStop).filter(RouteStop.student_id == student_id).first()
    if stop:
        stop.address = student.address
        stop.lat = lat
        stop.lon = lon
    else:
        max_order = db.query(RouteStop).filter(RouteStop.route_id == 1).count()
        stop = RouteStop(
            route_id=1,
            student_id=student.student_id,
            stop_order=max_order + 1,
            address=student.address,
            lat=lat,
            lon=lon,
            geometry="[]",
            distance_m=0.0,
            duration_s=0.0
        )
        db.add(stop)

    db.commit()
    logging.info(f"[Admin Action] Updated student profile: HS{student_id:04d} - {student.full_name}")
    return RedirectResponse(url="/students", status_code=303)

@app.post("/attendants/add")
def add_attendant(
    request: Request,
    attendant_id: str = Form(...),
    full_name: str = Form(...),
    citizen_id: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    rfid_code: str = Form(...),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    aid = attendant_id.strip()
    rfid = rfid_code.strip()
    cccd = citizen_id.strip()
    
    # Check duplicate Attendant ID
    existing_id = db.query(Attendant).filter(Attendant.attendant_id == aid).first()
    if existing_id:
        logging.warning(f"[Admin Verification] Attempt to register duplicate Attendant ID: {aid}")
        raise HTTPException(status_code=400, detail="Attendant ID is already registered!")
        
    # Check duplicate RFID in attendants
    existing_att = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
    if existing_att:
        logging.warning(f"[Admin Verification] Attempt to register duplicate attendant RFID: {rfid}")
        raise HTTPException(status_code=400, detail="RFID Card UID is already registered to another attendant!")
        
    # Check duplicate RFID in students
    existing_stud = db.query(Student).filter(Student.rfid_code == rfid).first()
    if existing_stud:
        logging.warning(f"[Admin Verification] Attempt to register attendant RFID already assigned to student: {rfid}")
        raise HTTPException(status_code=400, detail="RFID Card UID is already registered to a student!")

    attendant = Attendant(
        attendant_id=aid,
        full_name=full_name.strip(),
        citizen_id=cccd,
        phone=phone.strip(),
        email=email.strip(),
        rfid_code=rfid,
        status="active",
        duty_status="offduty"
    )
    db.add(attendant)
    db.commit()
    logging.info(f"[Admin Action] Registered new active attendant: {aid} - {full_name.strip()}")
    return RedirectResponse(url="/attendants", status_code=303)

@app.post("/attendants/{attendant_id}/delete")
def delete_attendant(request: Request, attendant_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendant = db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first()
    if attendant:
        name = attendant.full_name
        db.query(Vehicle).filter(Vehicle.active_attendant_id == attendant_id).update({"active_attendant_id": None})
        db.delete(attendant)
        db.commit()
        logging.info(f"[Admin Action] Deleted attendant: {attendant_id} - {name}")
    return RedirectResponse(url="/attendants", status_code=303)

@app.post("/attendants/{attendant_id}/suspend")
def suspend_attendant(request: Request, attendant_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendant = db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first()
    if attendant:
        attendant.status = "suspended"
        db.query(Vehicle).filter(Vehicle.active_attendant_id == attendant_id).update({"active_attendant_id": None})
        db.commit()
        logging.info(f"[Admin Action] Suspended attendant: {attendant_id} - {attendant.full_name}")
    return RedirectResponse(url="/attendants", status_code=303)

@app.post("/attendants/{attendant_id}/activate")
def activate_attendant(request: Request, attendant_id: str, db: Session = Depends(get_db)):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendant = db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first()
    if attendant:
        attendant.status = "active"
        db.commit()
        logging.info(f"[Admin Action] Activated attendant: {attendant_id} - {attendant.full_name}")
    return RedirectResponse(url="/attendants", status_code=303)

@app.post("/attendants/{attendant_id}/edit")
def edit_attendant(
    request: Request,
    attendant_id: str,
    full_name: str = Form(...),
    citizen_id: str = Form(""),
    phone: str = Form(""),
    email: str = Form(""),
    rfid_code: str = Form(...),
    db: Session = Depends(get_db)
):
    if request.cookies.get("manager_session") != "manager1":
        raise HTTPException(status_code=401, detail="Unauthorized")
    attendant = db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first()
    if not attendant:
        raise HTTPException(status_code=404, detail="Attendant not found")

    rfid = rfid_code.strip()
    if rfid != attendant.rfid_code:
        # Check duplicate RFID in attendants
        existing_att = db.query(Attendant).filter(Attendant.rfid_code == rfid, Attendant.attendant_id != attendant_id).first()
        if existing_att:
            raise HTTPException(status_code=400, detail="RFID Card UID is already registered to another attendant!")
        # Check duplicate RFID in students
        existing_stud = db.query(Student).filter(Student.rfid_code == rfid).first()
        if existing_stud:
            raise HTTPException(status_code=400, detail="RFID Card UID is already registered to a student!")
        attendant.rfid_code = rfid

    attendant.full_name = full_name.strip()
    attendant.citizen_id = citizen_id.strip()
    attendant.phone = phone.strip()
    attendant.email = email.strip()
    db.commit()
    logging.info(f"[Admin Action] Updated attendant profile: {attendant_id} - {attendant.full_name}")
    return RedirectResponse(url="/attendants", status_code=303)

# --- APIS FOR PARENTS AND UI FETCHES ---

@app.get("/api/vehicle/{vid}/status")
def get_vehicle_status(vid: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
        
    driver_name = ""
    driver_id = vehicle.active_driver_id
    if driver_id:
        drv = db.query(Driver).filter(Driver.driver_id == driver_id).first()
        if drv:
            driver_name = drv.full_name
    else:
        trip = db.query(TripSession).filter(TripSession.vehicle_id == vid).order_by(TripSession.id.desc()).first()
        if trip and trip.driver_id:
            driver_id = trip.driver_id
            drv = db.query(Driver).filter(Driver.driver_id == driver_id).first()
            if drv:
                driver_name = drv.full_name
            
    is_pending = getattr(vehicle, "session_pending_confirm", 0)
    if not is_pending:
        trip = db.query(TripSession).filter(TripSession.vehicle_id == vid, TripSession.session_state == 1).first()
        if trip:
            is_pending = 1

    return {
        "vehicle_id": vehicle.id,
        "driver_id": driver_id or "Unassigned",
        "driver_name": driver_name or "Pending Driver Verification",
        "session_pending_confirm": is_pending,
        "sos_active": getattr(vehicle, "sos_active", 0),
        "attendant_id": vehicle.active_attendant_id,
        "lat": vehicle.last_lat,
        "lon": vehicle.last_lon,
        "speed": vehicle.last_speed,
        "students_count": vehicle.students_onboard,
        "seats_bitmap": vehicle.seats_bitmap,
        "temperature": vehicle.last_temp,
        "humidity": vehicle.last_humi,
        "is_streaming": vehicle.is_streaming,
        "stream_url": vehicle.stream_url
    }

@app.post("/api/vehicle/{vid}/stream")
async def toggle_vehicle_stream(vid: str, request: Request, db: Session = Depends(get_db)):
    """Accept 'action' from query string OR from form body (FormData), as sent by dashboard JS"""
    # Try query string first
    action = request.query_params.get("action")
    if not action:
        # Try form body (multipart/form-data or application/x-www-form-urlencoded)
        try:
            form = await request.form()
            action = form.get("action")
        except Exception:
            pass
    if not action:
        raise HTTPException(status_code=400, detail="Missing 'action' parameter (start or stop)")

    vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
        
    if action == "start":
        command_data = {
            "action": "start_stream",
            "protocol": "mjpeg",
            "stream_host": "192.168.137.1",
            "stream_port": 8554
        }
        mqtt_listener.send_signed_reply(db, vid, 20, "command", command_data)
        return {"result": "success", "message": "Command start_stream sent"}
    elif action == "stop":
        command_data = {
            "action": "stop_stream"
        }
        mqtt_listener.send_signed_reply(db, vid, 20, "command", command_data)
        return {"result": "success", "message": "Command stop_stream sent"}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

@app.post("/api/admin/vehicle/{vid}/confirm_session")
def confirm_vehicle_session(vid: str, action: str, db: Session = Depends(get_db)):
    """
    Manager Agree / Disagree with pending session on Web Dashboard (Spec Section 2.8 & 2.9)
    action: "agree" or "disagree"
    """
    global mqtt_listener
    vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
        
    vehicle.session_pending_confirm = 0
    db.commit()
    
    now_ts = int(time.time())

    if action == "agree":
        driver_id = vehicle.active_driver_id
        driver_name = "Driver"
        phone_number = ""
        if driver_id:
            driver = db.query(Driver).filter(Driver.driver_id == driver_id).first()
            if driver:
                driver_name = driver.full_name
                phone_number = driver.phone or ""
                
        response_data = {
            "session_state": 2, # RESUMED (APPROVED)
            "driver_id": driver_id,
            "driver_full_name": driver_name,
            "phone_number": phone_number
        }
        log22 = EventLog(vehicle_id=vid, timestamp=now_ts, type=22, details=json.dumps(response_data))
        db.add(log22)
        db.commit()

        if mqtt_listener:
            mqtt_listener.send_signed_reply(db, vid, 22, "vehicle/status/response", response_data)
        return {"result": "success", "message": "Manager approved driver session. Resumed on device.", "phone_number": phone_number}
        
    elif action == "disagree":
        # Force logout active driver in DB
        vehicle.active_driver_id = None
        db.commit()
        
        response_data = {
            "session_state": 3 # REQUIRE_NEW_LOGIN (REJECTED)
        }
        log22 = EventLog(vehicle_id=vid, timestamp=now_ts, type=22, details=json.dumps(response_data))
        db.add(log22)
        db.commit()

        if mqtt_listener:
            mqtt_listener.send_signed_reply(db, vid, 22, "vehicle/status/response", response_data)
        return {"result": "success", "message": "Manager disapproved driver session. Forced re-login on device."}
    else:
        raise HTTPException(status_code=400, detail="Invalid action")

@app.post("/api/session/{vid}/resolve")
def resolve_session_alias(vid: str, request: Request, action: str = None, db: Session = Depends(get_db)):
    """F6: API chuẩn REST xử lý phiên tồn đọng (action: 'agree' hoặc 'disagree')."""
    # Hỗ trợ lấy action từ query params hoặc form hoặc JSON body
    if not action:
        try:
            # Thử parse JSON
            import asyncio
            body = getattr(request, "_json", None)
            if body and isinstance(body, dict):
                action = body.get("action")
        except Exception:
            pass
    if not action:
        action = request.query_params.get("action", "agree")
    return confirm_vehicle_session(vid=vid, action=action, db=db)

@app.get("/api/session/pending")
def get_pending_sessions(db: Session = Depends(get_db)):
    """F6: Lấy danh sách các xe có phiên làm việc tồn đọng đang chờ Admin duyệt."""
    vehicles = db.query(Vehicle).filter(Vehicle.session_pending_confirm == 1).all()
    results = []
    for v in vehicles:
        driver = db.query(Driver).filter(Driver.driver_id == v.active_driver_id).first() if v.active_driver_id else None
        attendant = db.query(Attendant).filter(Attendant.attendant_id == v.active_attendant_id).first() if v.active_attendant_id else None
        results.append({
            "vehicle_id": v.id,
            "driver_id": v.active_driver_id,
            "driver_name": driver.full_name if driver else "Tài xế cũ",
            "attendant_id": v.active_attendant_id,
            "attendant_name": attendant.full_name if attendant else "Phụ xe cũ",
            "lat": v.last_lat,
            "lon": v.last_lon
        })
    return {"pending_sessions": results, "total": len(results)}

@app.post("/api/admin/vehicle/{vid}/dismiss_sos")
def dismiss_sos(vid: str, db: Session = Depends(get_db)):
    """
    Manager acknowledges and dismisses the SOS alert on the Dashboard.
    """
    vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")
    vehicle.sos_active = 0
    db.commit()
    logging.info(f"[Admin Action] SOS alert dismissed for vehicle: {vid}")
    return {"result": "success", "message": f"SOS alert dismissed for vehicle {vid}."}

def gen_frames(stream_url: str):
    cap = cv2.VideoCapture(stream_url)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    
    # Try a shorter connection timeout via OpenCV parameters if supported
    # If not supported, standard timeout applies.
    try:
        retry_count = 0
        while True:
            success, frame = cap.read()
            if not success:
                time.sleep(0.1)
                retry_count += 1
                if retry_count > 50: # 5 seconds of no frames, exit
                    break
                continue
            
            retry_count = 0
            ret, buffer = cv2.imencode('.jpg', frame)
            if not ret:
                continue
            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
    finally:
        cap.release()

@app.get("/api/vehicle/{vid}/video_feed")
def video_feed(vid: str, db: Session = Depends(get_db)):
    vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
    if not vehicle or not vehicle.is_streaming or not vehicle.stream_url:
        raise HTTPException(status_code=400, detail="Video stream is not active")
        
    return StreamingResponse(
        gen_frames(vehicle.stream_url),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@app.post("/api/parent/login")
def parent_login(body: ParentLoginBody, db: Session = Depends(get_db)):
    student = db.query(Student).filter(
        Student.rfid_code == body.password,
        Student.parent_phone == body.phone
    ).first()
    
    if not student:
        raise HTTPException(status_code=400, detail="Số điện thoại hoặc mã thẻ học sinh không đúng!")
        
    return {"result": 1, "rfid_code": student.rfid_code}

@app.get("/api/parent/child/{rfid}")
def get_child_status(rfid: str, db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.rfid_code == rfid).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student RFID not found")
        
    is_on_bus = student.current_seat > 0
    status_text = "không ở trên xe"
    
    if student.status == "vacation":
        status_text = "Nghỉ học hôm nay"
    elif is_on_bus:
        status_text = "Đang trên xe"
    elif student.status:
        status_text = student.status
        
    bus_lat, bus_lon = 21.0021, 105.8462  # Fallback to ACTVN coordinates
    
    # Get active vehicle
    vehicle = db.query(Vehicle).first()
    if vehicle:
        bus_lat = vehicle.last_lat
        bus_lon = vehicle.last_lon

    # Active driver & assistant details
    driver_name = "Chưa xác định"
    driver_phone = "Chưa xác định"
    driver_email = "Chưa xác định"
    driver_status = "logout"
    
    assistant_name = "Chưa xác định"
    assistant_status = "logout"
    
    students_count = "không xác định"
    temp_val = "không xác định"
    humi_val = "không xác định"
    
    vehicle_plate = "29A-123.45"
    vehicle_status = "Xe đang dừng"
    
    if vehicle:
        vehicle_plate = vehicle.id
        if vehicle.active_driver_id:
            driver = db.query(Driver).filter(Driver.driver_id == vehicle.active_driver_id).first()
            if driver:
                driver_name = driver.full_name
                driver_phone = driver.phone or "Chưa xác định"
                driver_email = driver.email or "Chưa xác định"
                driver_status = "Đang trên xe"
                vehicle_status = "Xe đang di chuyển"
                
                # Check if this student is not onboard to show they are being picked up
                if not is_on_bus and student.status != "vacation":
                    vehicle_status = "Xe đang đến vị trí của bạn"
                    
        if vehicle.active_attendant_id:
            attendant = db.query(Attendant).filter(Attendant.attendant_id == vehicle.active_attendant_id).first()
            if attendant:
                assistant_name = attendant.full_name
                assistant_status = "Đang trên xe"
                
        if vehicle.active_driver_id:
            students_count = str(vehicle.students_onboard)
            temp_val = f"{vehicle.last_temp:.1f}"
            humi_val = f"{vehicle.last_humi:.1f}"
            
    return {
        "student_id": student.student_id,
        "full_name": student.full_name,
        "status": student.status,
        "current_seat": student.current_seat,
        "is_on_bus": is_on_bus,
        "status_description": status_text,
        "bus_latitude": bus_lat,
        "bus_longitude": bus_lon,
        # Extended fields for parent app UI tabs
        "student_class": student.student_class or "Chưa xác định",
        "parent_phone": student.parent_phone or "Chưa xác định",
        "address": student.address or "Chưa xác định",
        "school_name": "Học Viện Kỹ Thuật Mật Mã",
        "parent_name": "Hà Văn Mẹ",
        # Vehicle & Crew info
        "vehicle_plate": vehicle_plate,
        "vehicle_status": vehicle_status,
        "students_count": students_count,
        "temperature": temp_val,
        "humidity": humi_val,
        "driver_name": driver_name,
        "driver_phone": driver_phone,
        "driver_email": driver_email,
        "driver_status": driver_status,
        "assistant_name": assistant_name,
        "assistant_status": assistant_status
    }

@app.post("/api/parent/vacation")
def register_vacation(rfid_code: str = Form(...), status: str = Form(...), db: Session = Depends(get_db)):
    student = db.query(Student).filter(Student.rfid_code == rfid_code).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
        
    student.status = "vacation" if status.lower() == "vacation" else "không ở trên xe"
    if student.status == "vacation":
        student.current_seat = 0
    db.commit()
    return {"result": 1, "rfid_code": rfid_code, "status": student.status}


# ─────────────────────────────────────────────────────────────────────────────
# F1: Manual Attendance API
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/student/{student_id}/manual-attendance")
def manual_attendance_student(
    student_id: int,
    vehicle_id: str = Form(...),
    direction: str = Form(...),           # "board" | "alight"
    reason: str = Form(default="forgot_card"),
    confirmed_by: str = Form(default="admin"),
    seat_number: int = Form(default=0),
    rfid_code: str = Form(default=""),
    request: Request = None,
    db: Session = Depends(get_db)
):
    """F1: Điểm danh thủ công cho Học sinh quên thẻ.
    Dashboard gọi API này, Server validate rồi relay lệnh về Pi qua MQTT."""

    student = db.query(Student).filter(Student.student_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    # Xác định trip_phase hiện tại từ DB
    today_str = time.strftime("%Y-%m-%d")
    shift_str = "MORNING" if time.localtime().tm_hour < 12 else "AFTERNOON"
    trip_sess = db.query(TripSession).filter(
        TripSession.vehicle_id == vehicle_id,
        TripSession.trip_date == today_str,
        TripSession.trip_shift == shift_str
    ).first()

    trip_phase_str = trip_sess.trip_phase if trip_sess else "PICKUP"
    phase_map = {"PICKUP": 1, "ARRIVE_SCHOOL": 2, "DEPART_SCHOOL": 3, "DROPOFF": 4}
    trip_phase_num = phase_map.get(trip_phase_str, 1)

    # Validate hướng đi theo pha
    if direction == "board" and trip_phase_str not in ("PICKUP", "DEPART_SCHOOL"):
        raise HTTPException(status_code=400, detail=f"Cannot board in phase {trip_phase_str}")
    if direction == "alight" and trip_phase_str not in ("DROPOFF", "ARRIVE_SCHOOL"):
        raise HTTPException(status_code=400, detail=f"Cannot alight in phase {trip_phase_str}")

    now_ts = int(time.time())

    # Ghi ManualAttendanceLog
    log_entry = ManualAttendanceLog(
        target_type="student",
        target_id=str(student_id),
        vehicle_id=vehicle_id,
        trip_session_id=trip_sess.id if trip_sess else None,
        direction=direction,
        seat_number=seat_number,
        confirmed_by=confirmed_by,
        reason=reason,
        confirmed_at=now_ts
    )
    db.add(log_entry)

    # Cập nhật trạng thái học sinh trong DB
    if direction == "board":
        student.status = "Đang trên xe"
        student.current_seat = seat_number
    elif direction == "alight":
        student.status = "không ở trên xe"
        student.current_seat = 0

    if trip_sess:
        boarded_list = []
        try:
            boarded_list = json.loads(trip_sess.boarded_student_ids or "[]")
        except Exception:
            boarded_list = []
        if direction == "board" and student_id not in boarded_list:
            boarded_list.append(student_id)
        elif direction == "alight" and student_id in boarded_list:
            boarded_list.remove(student_id)
        trip_sess.boarded_student_ids = json.dumps(boarded_list)
        trip_sess.students_onboard = len(boarded_list)
        vehicle.students_onboard = len(boarded_list)

    db.commit()

    # Relay lệnh về Pi qua type=20 (sử dụng mqtt_listener_instance)
    global mqtt_listener
    if mqtt_listener and mqtt_listener.client:
        import hmac, hashlib
        psk_key = bytes.fromhex(vehicle.device_psk)
        payload_data = {
            "action": "manual_attendance",
            "target_type": "student",
            "target_id": str(student_id),
            "rfid_code": rfid_code or student.rfid_code or "",
            "seat_number": seat_number,
            "direction": direction,
            "trip_phase": trip_phase_num,
            "confirmed_by": confirmed_by,
            "reason": reason,
            "confirmed_at": now_ts
        }
        serialized = json.dumps(payload_data, separators=(',', ':'), sort_keys=True)
        sign_msg = f"20|{now_ts}|{vehicle_id}|{serialized}".encode('utf-8')
        sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
        envelope = {"type": 20, "ts": now_ts, "vid": vehicle_id, "data": payload_data, "sig": sig}
        topic = f"HaVanDat0105/{vehicle_id}/command"
        mqtt_listener.client.publish(topic, json.dumps(envelope), qos=1)

    return {
        "result": 1,
        "message": f"Điểm danh thủ công {'lên xe' if direction == 'board' else 'xuống xe'} cho {student.full_name} thành công",
        "student_id": student_id,
        "direction": direction
    }


@app.post("/api/attendant/{attendant_id}/manual-attendance")
def manual_attendance_attendant(
    attendant_id: str,
    vehicle_id: str = Form(...),
    direction: str = Form(...),           # "board" | "alight"
    reason: str = Form(default="forgot_card"),
    confirmed_by: str = Form(default="admin"),
    db: Session = Depends(get_db)
):
    """F1: Xác nhận thủ công cho Phụ xe quên thẻ.
    Ghế phụ xe luôn = 2, direction board=lên xe / alight=xuống xe."""

    attendant = db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first()
    if not attendant:
        raise HTTPException(status_code=404, detail="Attendant not found")

    vehicle = db.query(Vehicle).filter(Vehicle.id == vehicle_id).first()
    if not vehicle:
        raise HTTPException(status_code=404, detail="Vehicle not found")

    today_str = time.strftime("%Y-%m-%d")
    shift_str = "MORNING" if time.localtime().tm_hour < 12 else "AFTERNOON"
    trip_sess = db.query(TripSession).filter(
        TripSession.vehicle_id == vehicle_id,
        TripSession.trip_date == today_str,
        TripSession.trip_shift == shift_str
    ).first()

    now_ts = int(time.time())

    # Ghi ManualAttendanceLog
    log_entry = ManualAttendanceLog(
        target_type="attendant",
        target_id=attendant_id,
        vehicle_id=vehicle_id,
        trip_session_id=trip_sess.id if trip_sess else None,
        direction=direction,
        seat_number=2,           # ghế phụ xe cố định
        confirmed_by=confirmed_by,
        reason=reason,
        confirmed_at=now_ts
    )
    db.add(log_entry)

    if direction == "board":
        vehicle.active_attendant_id = attendant_id
    elif direction == "alight":
        vehicle.active_attendant_id = None

    db.commit()

    global mqtt_listener
    if mqtt_listener and mqtt_listener.client:
        import hmac, hashlib
        psk_key = bytes.fromhex(vehicle.device_psk)
        payload_data = {
            "action": "manual_attendance",
            "target_type": "attendant",
            "target_id": attendant_id,
            "rfid_code": attendant.rfid_code or "",
            "seat_number": 2,           # ghế phụ xe cố định
            "direction": direction,
            "trip_phase": 1,
            "confirmed_by": confirmed_by,
            "reason": reason,
            "confirmed_at": now_ts
        }
        serialized = json.dumps(payload_data, separators=(',', ':'), sort_keys=True)
        sign_msg = f"20|{now_ts}|{vehicle_id}|{serialized}".encode('utf-8')
        sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
        envelope = {"type": 20, "ts": now_ts, "vid": vehicle_id, "data": payload_data, "sig": sig}
        topic = f"HaVanDat0105/{vehicle_id}/command"
        mqtt_listener.client.publish(topic, json.dumps(envelope), qos=1)

    return {
        "result": 1,
        "message": f"Xác nhận {'lên xe' if direction == 'board' else 'xuống xe'} cho phụ xe {attendant.full_name} thành công",
        "attendant_id": attendant_id,
        "direction": direction
    }


# ─────────────────────────────────────────────────────────────────────────────
# F3: Emergency SOS API
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/emergency/active")
def get_active_emergencies(db: Session = Depends(get_db)):
    """Lấy danh sách các sự kiện SOS đang kích hoạt (RECEIVED hoặc ACKED)."""
    events = db.query(EmergencyEvent).filter(EmergencyEvent.status.in_(["RECEIVED", "ACKED"])).order_by(EmergencyEvent.created_at.desc()).all()
    results = []
    for e in events:
        v = db.query(Vehicle).filter(Vehicle.id == e.vehicle_id).first()
        results.append({
            "id": e.id,
            "sos_id": e.sos_id,
            "vehicle_id": e.vehicle_id,
            "triggered_by": e.triggered_by,
            "lat": e.lat,
            "lon": e.lon,
            "status": e.status,
            "created_at": e.created_at,
            "acked_at": e.acked_at,
            "acked_by": e.acked_by,
            "driver_name": v.active_driver_id if v else "Chưa rõ"
        })
    return {"total": len(results), "emergencies": results}


@app.post("/api/emergency/{sos_id}/acknowledge")
def acknowledge_emergency(
    sos_id: str,
    confirmed_by: str = Form(default="admin"),
    db: Session = Depends(get_db)
):
    """F3: Quản trị viên tiếp nhận/Acknowledge trường hợp khẩn cấp trên Dashboard.
    Cập nhật status=ACKED và gửi ACK stage=1 về Pi qua MQTT."""
    event = db.query(EmergencyEvent).filter(EmergencyEvent.sos_id == sos_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Emergency event not found")

    event.status = "ACKED"
    event.acked_at = int(time.time())
    event.acked_by = confirmed_by
    db.commit()

    # Gửi ACK stage=1 về Pi
    vehicle = db.query(Vehicle).filter(Vehicle.id == event.vehicle_id).first()
    global mqtt_listener
    if mqtt_listener and mqtt_listener.client and vehicle:
        import hmac, hashlib
        now_ts = int(time.time())
        psk_key = bytes.fromhex(vehicle.device_psk)
        payload_data = {
            "action": "sos_ack",
            "sos_id": sos_id,
            "stage": 1
        }
        serialized = json.dumps(payload_data, separators=(',', ':'), sort_keys=True)
        sign_msg = f"20|{now_ts}|{vehicle.id}|{serialized}".encode('utf-8')
        sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
        envelope = {"type": 20, "ts": now_ts, "vid": vehicle.id, "data": payload_data, "sig": sig}
        topic = f"HaVanDat0105/{vehicle.id}/command"
        mqtt_listener.client.publish(topic, json.dumps(envelope), qos=1)
        print(f"[API F3] Emergency {sos_id} ACKED by {confirmed_by} → sent stage=1 to vehicle {vehicle.id}")

    return {
        "result": 1,
        "message": f"Đã tiếp nhận xử lý trường hợp khẩn cấp {sos_id}",
        "sos_id": sos_id,
        "status": "ACKED"
    }


@app.post("/api/emergency/{sos_id}/resolve")
def resolve_emergency(
    sos_id: str,
    db: Session = Depends(get_db)
):
    """Đóng sự kiện khẩn cấp sau khi đã xử lý xong."""
    event = db.query(EmergencyEvent).filter(EmergencyEvent.sos_id == sos_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Emergency event not found")

    event.status = "RESOLVED"
    vehicle = db.query(Vehicle).filter(Vehicle.id == event.vehicle_id).first()
    if vehicle:
        # Kiểm tra xem xe còn sự kiện SOS nào chưa giải quyết không
        remaining = db.query(EmergencyEvent).filter(
            EmergencyEvent.vehicle_id == vehicle.id,
            EmergencyEvent.status.in_(["RECEIVED", "ACKED"])
        ).count()
        if remaining <= 1:
            vehicle.sos_active = 0
    db.commit()

    return {"result": 1, "message": f"Sự kiện khẩn cấp {sos_id} đã được đóng", "sos_id": sos_id}


# ─────────────────────────────────────────────────────────────────────────────
# F7: Xuất bảng trạng thái học sinh (Trip Report - CSV & Excel)
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/trip-report")
def export_trip_report(
    date: str = None,
    vehicle_id: str = None,
    shift: str = "ALL",
    format: str = "csv",
    db: Session = Depends(get_db)
):
    """
    F7: Xuất bảng trạng thái học sinh (CSV hoặc Excel .xlsx) theo chuyến/ngày.
    format: 'csv', 'xlsx', 'json'
    """
    import io
    import csv

    target_date = date.strip() if date else time.strftime("%Y-%m-%d")

    # 1. Lấy danh sách vắng mặt trong ngày
    absences = db.query(StudentAbsence).filter(StudentAbsence.absence_date == target_date).all()
    absent_student_ids = {a.student_id for a in absences}

    # 2. Lấy TripSession theo ngày (và xe nếu có chỉ định)
    session_query = db.query(TripSession).filter(TripSession.trip_date == target_date)
    if vehicle_id and vehicle_id != "ALL":
        session_query = session_query.filter(TripSession.vehicle_id == vehicle_id)
    if shift and shift != "ALL":
        session_query = session_query.filter(TripSession.trip_shift == shift)
    sessions = session_query.all()

    # Tập hợp các học sinh đã từng lên xe hôm nay
    boarded_in_sessions = set()
    for s in sessions:
        try:
            b_ids = json.loads(s.boarded_student_ids or "[]")
            for b in b_ids:
                boarded_in_sessions.add(int(b) if str(b).isdigit() else b)
        except Exception:
            pass

    # 3. Lấy toàn bộ danh sách học sinh
    students = db.query(Student).order_by(Student.student_id.asc()).all()

    rows = []
    stt = 1
    for st in students:
        seat_text = f"Ghế {st.current_seat}" if st.current_seat > 0 else "---"
        assigned_bus = vehicle_id if (vehicle_id and vehicle_id != "ALL") else (sessions[0].vehicle_id if sessions else "Bus-01")

        if st.status == "suspended":
            status_text = "Đình chỉ học"
        elif st.student_id in absent_student_ids:
            status_text = "Vắng mặt (Có phép)"
        elif st.status == "Đang trên xe" or st.current_seat > 0:
            status_text = "Đang trên xe"
        elif st.student_id in boarded_in_sessions:
            status_text = "Đã xuống xe"
        elif st.status == "không ở trên xe" and boarded_in_sessions:
            status_text = "Đã xuống xe"
        else:
            status_text = "Chưa lên xe"

        note = ""
        if st.student_id in absent_student_ids:
            note = "Phụ huynh báo nghỉ"

        rows.append({
            "stt": stt,
            "student_id": f"HS{st.student_id:04d}",
            "rfid_code": st.rfid_code or "---",
            "full_name": st.full_name,
            "student_class": st.student_class or "---",
            "parent_phone": st.parent_phone or "---",
            "address": st.address or "---",
            "status": status_text,
            "seat": seat_text,
            "vehicle_id": assigned_bus,
            "trip_date": target_date,
            "note": note
        })
        stt += 1

    # 4. Trả JSON nếu format == 'json' (phục vụ preview giao diện)
    if format == "json":
        return {
            "date": target_date,
            "vehicle_id": vehicle_id or "ALL",
            "shift": shift,
            "total_students": len(rows),
            "onboard_count": sum(1 for r in rows if r["status"] == "Đang trên xe"),
            "dropped_count": sum(1 for r in rows if r["status"] == "Đã xuống xe"),
            "absent_count": sum(1 for r in rows if "Vắng mặt" in r["status"]),
            "pending_count": sum(1 for r in rows if r["status"] == "Chưa lên xe"),
            "students": rows
        }

    # 5. Xuất Excel (.xlsx) với openpyxl
    if format == "xlsx":
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = f"DiemDanh_{target_date}"

            # Header tiêu đề
            ws.merge_cells("A1:K1")
            title_cell = ws["A1"]
            title_cell.value = f"BẢNG BÁO CÁO TRẠNG THÁI HỌC SINH ĐƯA ĐÓN - NGÀY {target_date}"
            title_cell.font = Font(name="Arial", size=14, bold=True, color="1E3A8A")
            title_cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[1].height = 35

            # Phụ đề
            ws.merge_cells("A2:K2")
            sub_cell = ws["A2"]
            sub_cell.value = f"Xe: {vehicle_id or 'Tất cả'} | Ca: {shift} | Thời gian xuất: {time.strftime('%H:%M:%S %d/%m/%Y')}"
            sub_cell.font = Font(name="Arial", size=10, italic=True, color="475569")
            sub_cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.row_dimensions[2].height = 20

            headers = [
                "STT", "Mã học sinh", "Mã thẻ RFID", "Họ và tên", "Lớp",
                "SĐT Phụ huynh", "Địa chỉ đón/trả", "Trạng thái", "Vị trí ghế",
                "Xe đưa đón", "Ghi chú"
            ]
            ws.append([]) # Blank row 3
            ws.append(headers) # Row 4
            ws.row_dimensions[4].height = 26

            header_fill = PatternFill(start_color="1E40AF", end_color="1E40AF", fill_type="solid")
            header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")
            thin_border = Border(
                left=Side(style='thin', color='CBD5E1'),
                right=Side(style='thin', color='CBD5E1'),
                top=Side(style='thin', color='CBD5E1'),
                bottom=Side(style='thin', color='CBD5E1')
            )

            for col_num, h in enumerate(headers, 1):
                cell = ws.cell(row=4, column=col_num)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center")

            status_colors = {
                "Đang trên xe": "DCFCE7",
                "Đã xuống xe": "F1F5F9",
                "Chưa lên xe": "FEF3C7",
                "Vắng mặt (Có phép)": "FEE2E2",
                "Đình chỉ học": "E2E8F0"
            }

            for row_idx, r in enumerate(rows, start=5):
                row_data = [
                    r["stt"], r["student_id"], r["rfid_code"], r["full_name"], r["student_class"],
                    r["parent_phone"], r["address"], r["status"], r["seat"],
                    r["vehicle_id"], r["note"]
                ]
                ws.append(row_data)
                ws.row_dimensions[row_idx].height = 22

                for col_idx in range(1, len(headers) + 1):
                    c = ws.cell(row=row_idx, column=col_idx)
                    c.border = thin_border
                    c.font = Font(name="Arial", size=10)
                    if col_idx in (1, 2, 5, 6, 9, 10):
                        c.alignment = Alignment(horizontal="center", vertical="center")
                    elif col_idx == 8:
                        c.alignment = Alignment(horizontal="center", vertical="center")
                        bg_color = status_colors.get(r["status"], "FFFFFF")
                        c.fill = PatternFill(start_color=bg_color, end_color=bg_color, fill_type="solid")
                        c.font = Font(name="Arial", size=10, bold=True)
                    else:
                        c.alignment = Alignment(horizontal="left", vertical="center")

            col_widths = {1: 6, 2: 14, 3: 16, 4: 24, 5: 10, 6: 15, 7: 35, 8: 18, 9: 12, 10: 14, 11: 22}
            for col_idx, width in col_widths.items():
                col_letter = openpyxl.utils.get_column_letter(col_idx)
                ws.column_dimensions[col_letter].width = width

            output = io.BytesIO()
            wb.save(output)
            output.seek(0)

            filename = f"BaoCao_TrangThai_HocSinh_{target_date}.xlsx"
            return StreamingResponse(
                output,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": f"attachment; filename={filename}"}
            )
        except Exception as e:
            logging.error(f"[Export Excel Error] Fallback to CSV: {e}")

    # 6. Mặc định: Xuất file CSV chuẩn UTF-8 có BOM
    output = io.StringIO()
    output.write("\ufeff") # UTF-8 BOM
    writer = csv.writer(output, delimiter=',', quoting=csv.QUOTE_MINIMAL)

    writer.writerow([
        "STT", "Mã học sinh", "Mã thẻ RFID", "Họ và tên", "Lớp",
        "Số điện thoại phụ huynh", "Địa chỉ đón/trả", "Trạng thái",
        "Vị trí ghế", "Xe đưa đón", "Ngày", "Ghi chú"
    ])

    for r in rows:
        writer.writerow([
            r["stt"], r["student_id"], r["rfid_code"], r["full_name"], r["student_class"],
            r["parent_phone"], r["address"], r["status"], r["seat"],
            r["vehicle_id"], r["trip_date"], r["note"]
        ])

    csv_data = output.getvalue().encode('utf-8')
    filename = f"BaoCao_TrangThai_HocSinh_{target_date}.csv"
    return StreamingResponse(
        io.BytesIO(csv_data),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )



