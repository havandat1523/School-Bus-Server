import hmac
import hashlib
import json
import time
import threading
import paho.mqtt.client as mqtt
import numpy as np
import urllib.request
from database import SessionLocal
from models import Vehicle, Driver, Attendant, Student, TelemetryLog, EventLog, TripSession, RouteStop, StudentAbsence, School, ManualAttendanceLog, EmergencyEvent
import os

# Load configuration values from environment or fallback
env_vars = {}
env_path = os.path.join(os.path.dirname(__file__), ".env")
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                env_vars[key.strip()] = val.strip()

MQTT_HOST = env_vars.get("MQTT_BROKER_HOST", os.getenv("MQTT_BROKER_HOST", "broker.hivemq.com"))
MQTT_PORT = int(env_vars.get("MQTT_BROKER_PORT", os.getenv("MQTT_BROKER_PORT", "1883")))

class ServerMQTTListener(threading.Thread):
    def __init__(self):
        super().__init__()
        self.daemon = True
        self.client = mqtt.Client(client_id="schoolbus_backend_server_core")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.running = True

    def run(self):
        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            self.client.loop_forever()
        except Exception as e:
            print(f"[MQTT Server] Failed to connect: {e}")

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[MQTT Server] Connected successfully to MQTT Broker.")
            # Subscribe to all vehicle streams (3, 4 and 5 level topics)
            self.client.subscribe("HaVanDat0105/+/+")
            self.client.subscribe("HaVanDat0105/+/+/+")
            self.client.subscribe("HaVanDat0105/+/+/+/+")  # for vehicle/status/request (5 levels)
        else:
            print(f"[MQTT Server] Connection failed (rc={rc})")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload_str = msg.payload.decode("utf-8")
        
        try:
            envelope = json.loads(payload_str)
        except json.JSONDecodeError:
            # Silently ignore non-JSON messages from other public broker users
            return
        except Exception as e:
            print(f"[MQTT Server] Error loading packet: {e}")
            return
            
        try:
            vid = envelope.get("vid")
            msg_type = envelope.get("type")
            ts = envelope.get("ts")
            data = envelope.get("data")
            sig = envelope.get("sig")
            
            # Avoid processing server-sent response topics (prevent self-loop)
            # Backend publishes on: vehicle/status/response, command — ignore those
            if any(topic.endswith(suffix) for suffix in ["/ack", "/status/response", "/command", "/vehicle/status/response"]):
                return

            if not vid or msg_type is None or ts is None or data is None or not sig:
                return

            db = SessionLocal()
            try:
                # 1. Fetch Vehicle PSK from DB
                vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
                if not vehicle:
                    # Auto-enroll vehicle for convenience of testing, with a default PSK
                    default_psk = "b7e2f1a9c4d8e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0"
                    vehicle = Vehicle(
                        id=vid,
                        device_psk=default_psk,
                        chassis_number="RE123456789"
                    )
                    db.add(vehicle)
                    db.commit()
                    print(f"[MQTT Server] Auto-enrolled new vehicle: {vid}")
                
                # 2. Verify Signature
                psk_key = bytes.fromhex(vehicle.device_psk)
                serialized_data = json.dumps(data, separators=(',', ':'), sort_keys=True)
                sign_msg = f"{msg_type}|{ts}|{vid}|{serialized_data}".encode('utf-8')
                
                expected_sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
                
                if not hmac.compare_digest(sig, expected_sig):
                    print(f"[SECURITY WARNING] Signature mismatch on topic {topic}! Packet dropped.")
                    return
                    
                # 3. Replay Protection (ignore if data is flagged is_replay)
                if not data.get("is_replay"):
                    server_time = int(time.time())
                    if abs(server_time - ts) > 30:
                        print(f"[SECURITY WARNING] Replay window expired! Ts: {ts}, Server time: {server_time}. Dropped.")
                        return

                # 4. Process valid payload
                self.process_payload(db, vid, msg_type, ts, data)
                
            finally:
                db.close()
                
        except Exception as e:
            print(f"[MQTT Server] Error processing payload: {e}")

    def process_payload(self, db, vid, msg_type, ts, data):
        # Update event log
        event_log = EventLog(
            vehicle_id=vid,
            timestamp=ts,
            type=msg_type,
            details=json.dumps(data)
        )
        db.add(event_log)
        
        # Get vehicle reference
        vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()

        # Handle specific message type
        if msg_type == 1: # driver_login_request
            captured_vec = data.get("face_vector")
            response_data = {"result": 0}
            
            if captured_vec:
                # Strictly query ONLY drivers with status == "active" (suspended/inactive drivers CANNOT log in)
                active_drivers = db.query(Driver).filter(Driver.status == "active").all()
                
                best_match = None
                min_dist = 999.0
                cap_arr = np.array(captured_vec)
                
                for driver in active_drivers:
                    if not driver.face_vector_json:
                        # Active driver has no face vector enrolled yet. Auto-enroll real face vector!
                        print(f"[MQTT Server] Active driver {driver.full_name} ({driver.driver_id}) has no biometric vector. Enrolling real face vector now!")
                        driver.face_vector_json = json.dumps(captured_vec)
                        db.commit()
                        best_match = driver
                        break

                    cached_vec = np.array(json.loads(driver.face_vector_json))
                    is_mock_vector = len(cached_vec) == 128 and abs(cached_vec[1] - 0.005) < 0.001
                    
                    if is_mock_vector:
                        # If active driver has an initial seed/mock vector, replace with real face vector on first scan
                        print(f"[MQTT Server] Enrolling real face vector for active driver {driver.full_name} ({driver.driver_id}).")
                        driver.face_vector_json = json.dumps(captured_vec)
                        db.commit()
                        best_match = driver
                        break

                    dist = np.linalg.norm(cap_arr - cached_vec)
                    print(f"[MQTT Server] Biometric check for active driver {driver.full_name} ({driver.driver_id}): distance={dist:.4f}")
                    
                    # Strict distance check (Euclidean distance < 0.6)
                    if dist < 0.6 and dist < min_dist:
                        min_dist = dist
                        best_match = driver
                            
                if best_match:
                    print(f"[MQTT Server] Active driver {best_match.full_name} ({best_match.driver_id}) logged in successfully.")
                    vehicle.active_driver_id = best_match.driver_id
                    db.commit()
                    
                    response_data = {
                        "driver_id": best_match.driver_id,
                        "result": 1,
                        "full_name": best_match.full_name,
                        "phone_number": best_match.phone or ""
                    }
                else:
                    print(f"[MQTT Server] Driver login REJECTED: No active driver matched biometric face vector or status is suspended.")
                    
            self.send_signed_reply(db, vid, 2, "driver/login/ack", response_data)

        elif msg_type == 3: # driver_logout_request
            vehicle.active_driver_id = None
            db.commit()
            self.send_signed_reply(db, vid, 4, "driver/logout/ack", {"result": 1})

        elif msg_type in (6, 11): # RFID Scanned via Shared Reader on Slave Node
            rfid = data.get("rfid_code")
            seat_num = data.get("seat_number", 0)
            phase = data.get("trip_phase", 1) # 1=PICKUP, 2=ARRIVE_SCHOOL, 3=DEPART_SCHOOL, 4=DROPOFF
            action_hint = data.get("action", "") # F2: "validate_only" = chỉ validate, chưa finalize

            # ── F2: Validate-only request (Pi gửi để hỏi Server trước khi ngồi ghế) ──
            if action_hint == "validate_only":
                # 1. Kiểm tra phụ xe
                attendant = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
                if attendant:
                    # Phụ xe hợp lệ → trả rfid_check_ack result=1, is_attendant=True
                    # Đồng thời xử lý login/logout phụ xe
                    if vehicle.active_attendant_id == attendant.attendant_id:
                        vehicle.active_attendant_id = None
                        db.commit()
                        print(f"[MQTT Server F2] Attendant {attendant.full_name} LOGGED OUT via RFID validate.")
                        self.send_signed_reply(db, vid, 20, "command", {
                            "action": "rfid_check_ack",
                            "rfid_code": rfid,
                            "result": 1,
                            "is_attendant": True,
                            "attendant_action": "logout",
                            "attendant_id": attendant.attendant_id
                        })
                    else:
                        vehicle.active_attendant_id = attendant.attendant_id
                        db.commit()
                        print(f"[MQTT Server F2] Attendant {attendant.full_name} LOGGED IN via RFID validate.")
                        self.send_signed_reply(db, vid, 20, "command", {
                            "action": "rfid_check_ack",
                            "rfid_code": rfid,
                            "result": 1,
                            "is_attendant": True,
                            "attendant_action": "login",
                            "attendant_id": attendant.attendant_id,
                            "full_name": attendant.full_name
                        })
                    return

                # 2. Kiểm tra học sinh
                student = db.query(Student).filter(Student.rfid_code == rfid).first()
                if not student:
                    print(f"[MQTT Server F2] Unknown RFID {rfid} — reject")
                    self.send_signed_reply(db, vid, 20, "command", {
                        "action": "rfid_check_ack",
                        "rfid_code": rfid,
                        "result": 0,
                        "reason": "unknown_rfid"
                    })
                    return

                # Học sinh hợp lệ → trả rfid_check_ack result=1
                print(f"[MQTT Server F2] Student {student.full_name} RFID valid — waiting for seat")
                self.send_signed_reply(db, vid, 20, "command", {
                    "action": "rfid_check_ack",
                    "rfid_code": rfid,
                    "result": 1,
                    "is_attendant": False,
                    "student_id": student.student_id,
                    "full_name": student.full_name
                })
                return

            # ── Luồng cũ: type 11 finalize (sau khi học sinh ngồi vào ghế) ──
            # 1. Check if RFID belongs to an Attendant (Phụ xe)
            attendant = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
            if attendant:
                if vehicle.active_attendant_id == attendant.attendant_id:
                    # Already logged in -> Perform LOGOUT
                    vehicle.active_attendant_id = None
                    db.commit()
                    print(f"[Server DB] Attendant {attendant.full_name} ({attendant.attendant_id}) LOGGED OUT via shared RFID reader.")
                    self.send_signed_reply(db, vid, 9, "attendant/logout/ack", {
                        "result": 1,
                        "attendant_id": attendant.attendant_id
                    })
                    return
                else:
                    # Not logged in -> Perform LOGIN
                    vehicle.active_attendant_id = attendant.attendant_id
                    db.commit()
                    print(f"[Server DB] Attendant {attendant.full_name} ({attendant.attendant_id}) LOGGED IN via shared RFID reader.")
                    self.send_signed_reply(db, vid, 7, "attendant/login/ack", {
                        "attendant_id": attendant.attendant_id,
                        "result": 1,
                        "full_name": attendant.full_name,
                        "rfid_code": rfid
                    })
                    return

            # 2. Check if RFID belongs to a Student (Học sinh)
            student = db.query(Student).filter(Student.rfid_code == rfid).first()
            if not student:
                print(f"[Server DB] Unknown RFID card scanned: {rfid}. Sending error event 05/002.")
                self.send_signed_reply(db, vid, 12, "student/event", {
                    "event_code": 502,
                    "message": "Invalid RFID card",
                    "rfid_code": rfid
                })
                return

            # Student valid -> Process Boarding & TripSession sync
            today_str = time.strftime("%Y-%m-%d")
            shift_str = "MORNING" if time.localtime().tm_hour < 12 else "AFTERNOON"
            trip_sess = db.query(TripSession).filter(
                TripSession.vehicle_id == vid,
                TripSession.trip_date == today_str,
                TripSession.trip_shift == shift_str
            ).first()
            if not trip_sess:
                trip_sess = TripSession(
                    vehicle_id=vid,
                    trip_date=today_str,
                    trip_shift=shift_str,
                    trip_phase="PICKUP" if shift_str == "MORNING" else "DROPOFF",
                    students_onboard=0,
                    boarded_student_ids="[]"
                )
                db.add(trip_sess)
                db.commit()

            boarded_list = []
            try:
                boarded_list = json.loads(trip_sess.boarded_student_ids or "[]")
            except Exception:
                boarded_list = []

            if phase == 1: # PICKUP
                student.current_seat = seat_num
                student.status = "Đang trên xe"
                if student.student_id not in boarded_list:
                    boarded_list.append(student.student_id)
            elif phase == 4: # DROPOFF
                student.current_seat = 0
                student.status = "không ở trên xe"
                if student.student_id in boarded_list:
                    boarded_list.remove(student.student_id)

            trip_sess.boarded_student_ids = json.dumps(boarded_list)
            trip_sess.students_onboard = len(boarded_list)
            vehicle.students_onboard = len(boarded_list)
            db.commit()
            print(f"[Server DB] Student {student.full_name} (ID={student.student_id}): Seat={student.current_seat}, Status={student.status}, Phase={phase}, Onboard={len(boarded_list)}")

            # 3. Dynamic Routing & Next Stop Destination calculation (type=23 student_scan_ack)
            absent_ids = {a.student_id for a in db.query(StudentAbsence).filter(StudentAbsence.absence_date == today_str).all()}
            all_stops = db.query(RouteStop).filter(RouteStop.student_id != None).order_by(RouteStop.stop_order.asc()).all()

            cur_lat = vehicle.last_lat if vehicle.last_lat else 21.0021
            cur_lon = vehicle.last_lon if vehicle.last_lon else 105.8462

            next_stop = None
            next_student = None
            remaining = 0

            if phase in (1, 4):
                for stop in all_stops:
                    if stop.student_id in absent_ids:
                        continue # Skip absent students
                    st = db.query(Student).filter(Student.student_id == stop.student_id).first()
                    if not st:
                        continue
                    target_condition = (st.student_id not in boarded_list) if phase == 1 else (st.student_id in boarded_list)
                    if target_condition:
                        if not next_stop:
                            next_stop = stop
                            next_student = st
                        remaining += 1

            school = db.query(School).first()
            school_lat = school.lat if school and school.lat else 20.9806
            school_lon = school.lon if school and school.lon else 105.7958
            school_addr = school.address if school else "141 Chiến Thắng, Tân Triều, Thanh Trì, Hà Nội"

            if next_stop and next_student:
                dest_lat = next_stop.lat if next_stop.lat else 21.0105
                dest_lon = next_stop.lon if next_stop.lon else 105.8501
                route_geom = self.calculate_dynamic_route(cur_lat, cur_lon, dest_lat, dest_lon)
                
                ack_data = {
                    "next_student_id": f"HS{next_student.student_id:04d}",
                    "next_student_name": next_student.full_name,
                    "next_address": next_stop.address,
                    "next_lat": dest_lat,
                    "next_lon": dest_lon,
                    "next_route_geometry": route_geom,
                    "is_last_student_picked": False,
                    "students_onboard": len(boarded_list),
                    "students_remaining": remaining
                }
            else:
                # All students picked/dropped -> Route back to School
                route_geom = self.calculate_dynamic_route(cur_lat, cur_lon, school_lat, school_lon)
                ack_data = {
                    "next_student_id": None,
                    "next_student_name": "Đang về trường" if phase == 1 else "Hoàn thành chuyến",
                    "next_address": school_addr,
                    "next_lat": school_lat,
                    "next_lon": school_lon,
                    "next_route_geometry": route_geom,
                    "is_last_student_picked": (phase == 1),
                    "students_onboard": len(boarded_list),
                    "students_remaining": 0
                }

            self.send_signed_reply(db, vid, 23, "student/scan/ack", ack_data)

        elif msg_type == 8: # attendant_logout_request (explicit)
            vehicle.active_attendant_id = None
            db.commit()
            self.send_signed_reply(db, vid, 9, "attendant/logout/ack", {"result": 1})


        elif msg_type == 13: # seat_status
            seats = data.get("seats", {})
            
            bitmap = 0
            student_count = 0
            for seat_str, occupied in seats.items():
                seat_idx = int(seat_str)
                if occupied == 1:
                    bitmap |= (1 << (seat_idx - 1))
                    if seat_idx >= 3:
                        student_count += 1
            
            vehicle.seats_bitmap = bitmap
            vehicle.students_onboard = student_count
            db.commit()
            
        elif msg_type == 14: # telemetry
            lat = data.get("lat", 0.0)
            lon = data.get("lon", 0.0)
            speed = data.get("speed_kmh", 0.0)
            students = data.get("students_onboard", 0)
            temp = data.get("temperature", 0.0)
            humi = data.get("humidity", 0.0)
            
            # Update Vehicle positions
            vehicle.last_lat = lat
            vehicle.last_lon = lon
            vehicle.last_speed = speed
            vehicle.students_onboard = students
            vehicle.last_temp = temp
            vehicle.last_humi = humi
            
            # Create Telemetry Record
            log = TelemetryLog(
                vehicle_id=vid,
                timestamp=ts,
                lat=lat,
                lon=lon,
                speed=speed,
                student_count=students,
                temp=temp,
                humi=humi
            )
            db.add(log)
            db.commit()

        elif msg_type == 15: # emergency_sos
            sos_id = data.get("sos_id")
            if not sos_id:
                sos_id = str(__import__('uuid').uuid4())
            lat = data.get("lat", 0.0)
            lon = data.get("lon", 0.0)
            triggered_by = data.get("triggered_by", 1) # 1=slave, 2=ui, 3=gpio
            seat_number = data.get("seat_number", 0)
            driver_id = vehicle.active_driver_id if vehicle else None

            # 1. Khử trùng lặp qua sos_id: Chỉ tạo 1 EmergencyEvent dù nhận nhiều lần (retry)
            existing = db.query(EmergencyEvent).filter(EmergencyEvent.sos_id == sos_id).first()
            if not existing:
                now_ts = int(time.time())
                event = EmergencyEvent(
                    sos_id=sos_id,
                    vehicle_id=vid,
                    triggered_by=triggered_by,
                    lat=lat,
                    lon=lon,
                    status="RECEIVED",
                    created_at=now_ts
                )
                db.add(event)
                print(f"[MQTT Server F3] NEW SOS ALERT recorded: ID={sos_id} VID={vid} Src={triggered_by} Lat={lat} Lon={lon}")
            else:
                print(f"[MQTT Server F3] Duplicate SOS retry received for ID={sos_id} — sending ACK stage 0")

            # Mark vehicle as SOS Active in DB — Dashboard will pick this up and show alert
            if vehicle:
                vehicle.sos_active = 1
                if lat:
                    vehicle.last_lat = lat
                if lon:
                    vehicle.last_lon = lon
            db.commit()

            # 2. Gửi ngay ACK stage=0 về Pi để thiết bị dừng timer và phát 07/002
            self.send_signed_reply(db, vid, 20, "command", {
                "action": "sos_ack",
                "sos_id": sos_id,
                "stage": 0
            })

        elif msg_type == 20: # server_command ACK / stream_status / manual_attendance_ack
            action = data.get("action")
            if action == "stream_status":
                status = data.get("status", "inactive")
                url = data.get("stream_url", "")
                vehicle.is_streaming = 1 if status == "active" else 0
                vehicle.stream_url = url
                db.commit()
                print(f"[MQTT Server] Video stream status updated for {vid}: status={status}, url={url}")

            elif action == "manual_attendance_ack":
                # Pi xác nhận đã xử lý lệnh điểm danh thủ công
                result = data.get("result", 0)
                target_type = data.get("target_type", "student")
                target_id   = data.get("target_id", "")
                direction   = data.get("direction", "board")
                print(f"[MQTT Server] Manual attendance ACK from device: {target_type}/{target_id} dir={direction} result={result}")

        # ── F1: Manual attendance request từ Dashboard (Server → Pi) ──────────
        elif msg_type == 24: # manual_attendance_request (Server gửi lệnh về Pi)
            # Lệnh này được gửi khi admin bấm "Điểm danh thủ công" trên Dashboard
            # Server relay lệnh về Pi qua type=20 action=manual_attendance
            target_type  = data.get("target_type", "student")
            target_id    = data.get("target_id", "")
            direction    = data.get("direction", "board")
            seat_number  = data.get("seat_number", 0)
            rfid_code    = data.get("rfid_code", "")
            confirmed_by = data.get("confirmed_by", "admin")
            reason       = data.get("reason", "forgot_card")
            confirmed_at = data.get("confirmed_at", int(time.time()))
            trip_phase_num = data.get("trip_phase", 1)

            # Ghi ManualAttendanceLog
            today_str = time.strftime("%Y-%m-%d")
            shift_str = "MORNING" if time.localtime().tm_hour < 12 else "AFTERNOON"
            trip_sess = db.query(TripSession).filter(
                TripSession.vehicle_id == vid,
                TripSession.trip_date == today_str,
                TripSession.trip_shift == shift_str
            ).first()

            log_entry = ManualAttendanceLog(
                target_type=target_type,
                target_id=str(target_id),
                vehicle_id=vid,
                trip_session_id=trip_sess.id if trip_sess else None,
                direction=direction,
                seat_number=seat_number,
                confirmed_by=confirmed_by,
                reason=reason,
                confirmed_at=confirmed_at
            )
            db.add(log_entry)

            # Cập nhật DB phía Server nếu target là học sinh
            if target_type == "student" and trip_sess:
                boarded_list = []
                try:
                    boarded_list = __import__('json').loads(trip_sess.boarded_student_ids or "[]")
                except Exception:
                    boarded_list = []

                student = db.query(Student).filter(Student.student_id == int(target_id)).first() if target_id.isdigit() else None
                if not student and rfid_code:
                    student = db.query(Student).filter(Student.rfid_code == rfid_code).first()

                if student:
                    if direction == "board":
                        student.status = "Đang trên xe"
                        student.current_seat = seat_number
                        if student.student_id not in boarded_list:
                            boarded_list.append(student.student_id)
                    elif direction == "alight":
                        student.status = "không ở trên xe"
                        student.current_seat = 0
                        if student.student_id in boarded_list:
                            boarded_list.remove(student.student_id)
                    trip_sess.boarded_student_ids = __import__('json').dumps(boarded_list)
                    trip_sess.students_onboard = len(boarded_list)
                    vehicle.students_onboard = len(boarded_list)

            elif target_type == "attendant" and direction == "board":
                vehicle.active_attendant_id = str(target_id)
            elif target_type == "attendant" and direction == "alight":
                vehicle.active_attendant_id = None

            db.commit()
            print(f"[MQTT Server F1] Manual attendance logged: {target_type}/{target_id} dir={direction} by={confirmed_by}")

            # Relay lệnh về Pi
            self.send_signed_reply(db, vid, 20, "command", {
                "action": "manual_attendance",
                "target_type": target_type,
                "target_id": str(target_id),
                "rfid_code": rfid_code,
                "seat_number": seat_number,
                "direction": direction,
                "trip_phase": trip_phase_num,
                "confirmed_by": confirmed_by,
                "reason": reason,
                "confirmed_at": confirmed_at
            })

        elif msg_type == 21: # vehicle_status_request
            # Re-fetch vehicle fresh to get latest DB state
            vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
            driver = None
            if vehicle and vehicle.active_driver_id:
                driver = db.query(Driver).filter(Driver.driver_id == vehicle.active_driver_id).first()

            if not vehicle or not vehicle.active_driver_id:
                if vehicle:
                    vehicle.session_pending_confirm = 0
                    db.commit()
                reply_data = {"session_state": 0} # NONE — no previous session
                print(f"[MQTT Server] vehicle_status_request for {vid}: No active driver → session_state=0 (NONE)")
            else:
                # Has active_driver_id → require Admin Agree/Disagree on Dashboard
                vehicle.session_pending_confirm = 1
                db.commit()
                driver_full_name = driver.full_name if driver else "Driver"
                reply_data = {
                    "session_state": 1, # PENDING_CONFIRM — Admin must Agree/Disagree
                    "driver_id": vehicle.active_driver_id,
                    "driver_full_name": driver_full_name,
                    "existing_driver_id": vehicle.active_driver_id,
                    "existing_driver_name": driver_full_name,
                    "phone_number": driver.phone if driver and driver.phone else ""
                }
                print(f"[MQTT Server] vehicle_status_request for {vid}: Found active driver '{driver_full_name}' → session_state=1 (PENDING_CONFIRM). session_pending_confirm=1 saved to DB.")

            # Log Type 22 response into EventLog
            now_ts = int(time.time())
            log22 = EventLog(vehicle_id=vid, timestamp=now_ts, type=22, details=json.dumps(reply_data))
            db.add(log22)
            db.commit()

            self.send_signed_reply(db, vid, 22, "vehicle/status/response", reply_data)

        db.commit()

    def send_signed_reply(self, db, vid: str, msg_type: int, sub_topic: str, data: dict):
        """
        Signs the response packet and publishes it back to the vehicle.
        """
        vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
        if not vehicle:
            return
            
        timestamp = int(time.time())
        psk_key = bytes.fromhex(vehicle.device_psk)
        
        serialized_data = json.dumps(data, separators=(',', ':'), sort_keys=True)
        sign_msg = f"{msg_type}|{timestamp}|{vid}|{serialized_data}".encode('utf-8')
        sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
        
        envelope = {
            "type": msg_type,
            "ts": timestamp,
            "vid": vid,
            "data": data,
            "sig": sig
        }
        
        reply_topic = f"HaVanDat0105/{vid}/{sub_topic}"
        self.client.publish(reply_topic, json.dumps(envelope), qos=1)
        print(f"[MQTT Server] Sent signed reply type {msg_type} on {reply_topic}")

    def calculate_dynamic_route(self, lat1: float, lon1: float, lat2: float, lon2: float) -> list:
        """
        Calls OSRM routing engine to get dynamic route geometry.
        Fallback to direct line coordinates [[lon1, lat1], [lon2, lat2]] if network/API fails.
        """
        fallback_geom = [[float(lon1), float(lat1)], [float(lon2), float(lat2)]]
        if abs(lat1) < 0.1 or abs(lon1) < 0.1 or abs(lat2) < 0.1 or abs(lon2) < 0.1:
            return fallback_geom
        
        url = f"http://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}?overview=full&geometries=geojson"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "SchoolBusServer/1.3.0"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                res_data = json.loads(resp.read().decode())
                if res_data.get("code") == "Ok" and res_data.get("routes"):
                    coords = res_data["routes"][0]["geometry"]["coordinates"] # [[lon, lat], ...]
                    if coords and len(coords) > 1:
                        return coords
        except Exception as e:
            print(f"[MQTT Server] OSRM dynamic routing error/timeout: {e}. Fallback to direct segment.")
            
        return fallback_geom
