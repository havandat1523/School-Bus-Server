import hmac
import hashlib
import json
import time
import threading
import paho.mqtt.client as mqtt
import numpy as np
from database import SessionLocal
from models import Vehicle, Driver, Attendant, Student, TelemetryLog, EventLog
import os

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
        self.client = mqtt.Client(client_id="schoolbus_gateway_receiver")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.running = True

    def run(self):
        try:
            self.client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            self.client.loop_forever()
        except Exception as e:
            print(f"[MQTT Gateway] Failed to connect: {e}")

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            print("[MQTT Gateway] Connected successfully to MQTT Broker.")
            self.client.subscribe("HaVanDat0105/#")
        else:
            print(f"[MQTT Gateway] Connection failed (rc={rc})")

    def on_message(self, client, userdata, msg):
        topic = msg.topic
        payload_str = msg.payload.decode("utf-8")
        
        try:
            envelope = json.loads(payload_str)
        except json.JSONDecodeError:
            # Silently ignore non-JSON messages on the public broker
            return
        except Exception as e:
            print(f"[MQTT Gateway] Error loading packet: {e}")
            return
            
        try:
            vid = envelope.get("vid")
            msg_type = envelope.get("type")
            ts = envelope.get("ts")
            data = envelope.get("data")
            sig = envelope.get("sig")
            
            if topic.endswith("/ack"):
                return
                
            if not vid or msg_type is None or ts is None or data is None or not sig:
                return

            db = SessionLocal()
            try:
                vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()
                if not vehicle:
                    default_psk = "b7e2f1a9c4d8e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0"
                    vehicle = Vehicle(
                        id=vid,
                        device_psk=default_psk,
                        chassis_number="RE123456789"
                    )
                    db.add(vehicle)
                    db.commit()
                    print(f"[MQTT Gateway] Auto-enrolled new vehicle: {vid}")
                
                psk_key = bytes.fromhex(vehicle.device_psk)
                serialized_data = json.dumps(data, separators=(',', ':'), sort_keys=True)
                sign_msg = f"{msg_type}|{ts}|{vid}|{serialized_data}".encode('utf-8')
                
                expected_sig = hmac.new(psk_key, sign_msg, hashlib.sha256).hexdigest()
                
                if not hmac.compare_digest(sig, expected_sig):
                    print(f"[SECURITY WARNING] Signature mismatch on topic {topic}! Packet dropped.")
                    return
                    
                if not data.get("is_replay"):
                    server_time = int(time.time())
                    if abs(server_time - ts) > 30:
                        print(f"[SECURITY WARNING] Replay window expired! Ts: {ts}, Server time: {server_time}. Dropped.")
                        return

                self.process_payload(db, vid, msg_type, ts, data)
                
            finally:
                db.close()
                
        except Exception as e:
            print(f"[MQTT Gateway] Error processing payload: {e}")

    def process_payload(self, db, vid, msg_type, ts, data):
        event_log = EventLog(
            vehicle_id=vid,
            timestamp=ts,
            type=msg_type,
            details=json.dumps(data)
        )
        db.add(event_log)
        
        vehicle = db.query(Vehicle).filter(Vehicle.id == vid).first()

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
                        print(f"[Gateway DB] Active driver {driver.full_name} ({driver.driver_id}) has no biometric vector. Enrolling real face vector now!")
                        driver.face_vector_json = json.dumps(captured_vec)
                        db.commit()
                        best_match = driver
                        break

                    cached_vec = np.array(json.loads(driver.face_vector_json))
                    is_mock_vector = len(cached_vec) == 128 and abs(cached_vec[1] - 0.005) < 0.001
                    
                    if is_mock_vector:
                        # If active driver has an initial seed/mock vector, replace with real face vector on first scan
                        print(f"[Gateway DB] Enrolling real face vector for active driver {driver.full_name} ({driver.driver_id}).")
                        driver.face_vector_json = json.dumps(captured_vec)
                        db.commit()
                        best_match = driver
                        break

                    dist = np.linalg.norm(cap_arr - cached_vec)
                    print(f"[Gateway DB] Biometric check for active driver {driver.full_name} ({driver.driver_id}): distance={dist:.4f}")
                    
                    # Strict distance check (Euclidean distance < 0.6)
                    if dist < 0.6 and dist < min_dist:
                        min_dist = dist
                        best_match = driver
                            
                if best_match:
                    print(f"[Gateway DB] Active driver {best_match.full_name} ({best_match.driver_id}) logged in successfully.")
                    vehicle.active_driver_id = best_match.driver_id
                    db.commit()
                    
                    response_data = {
                        "driver_id": best_match.driver_id,
                        "result": 1,
                        "full_name": best_match.full_name
                    }
                else:
                    print(f"[Gateway DB] Driver login REJECTED: No active driver matched biometric face vector or status is suspended.")
                    
            self.send_signed_reply(db, vid, 2, "driver/login/ack", response_data)

        elif msg_type == 3: # driver_logout_request
            vehicle.active_driver_id = None
            db.commit()
            self.send_signed_reply(db, vid, 4, "driver/logout/ack", {"result": 1})

        elif msg_type == 6: # attendant_login_request
            rfid = data.get("rfid_code")
            response_data = {"result": 0}
            
            attendant = db.query(Attendant).filter(Attendant.rfid_code == rfid).first()
            if attendant:
                if getattr(attendant, "status", "active") == "suspended":
                    print(f"[Gateway DB WARNING] Attendant {attendant.full_name} ({attendant.attendant_id}) IS SUSPENDED! Login rejected.")
                    response_data = {"result": 0, "message": "Phụ xe bị tạm hoãn/đình chỉ!"}
                else:
                    print(f"[Gateway DB] Attendant {attendant.full_name} logged in.")
                    vehicle.active_attendant_id = attendant.attendant_id
                    attendant.duty_status = "onduty"
                    db.commit()
                    
                    response_data = {
                        "attendant_id": attendant.attendant_id,
                        "result": 1,
                        "full_name": attendant.full_name,
                        "rfid_code": rfid
                    }
            self.send_signed_reply(db, vid, 7, "attendant/login/ack", response_data)

        elif msg_type == 8: # attendant_logout_request
            if vehicle.active_attendant_id:
                att = db.query(Attendant).filter(Attendant.attendant_id == vehicle.active_attendant_id).first()
                if att:
                    att.duty_status = "offduty"
            vehicle.active_attendant_id = None
            db.commit()
            self.send_signed_reply(db, vid, 9, "attendant/logout/ack", {"result": 1})

        elif msg_type == 11: # student_scan
            rfid = data.get("rfid_code")
            seat_num = data.get("seat_number", 0)
            phase = data.get("trip_phase", 1)
            
            student = db.query(Student).filter(Student.rfid_code == rfid).first()
            if student:
                if getattr(student, "status", "active") == "suspended":
                    print(f"[Gateway DB WARNING] Student {student.full_name} ({student.student_id}) IS SUSPENDED! Card scan rejected.")
                else:
                    if phase == 1:
                        student.current_seat = seat_num
                        student.status = "Đang trên xe"
                    elif phase == 4:
                        student.current_seat = 0
                        student.status = "không ở trên xe"
                    db.commit()
                    print(f"[Gateway DB] Student {student.full_name} scanned card: Seat={student.current_seat}, Status={student.status}")

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
            
            vehicle.last_lat = lat
            vehicle.last_lon = lon
            vehicle.last_speed = speed
            vehicle.students_onboard = students
            vehicle.last_temp = temp
            vehicle.last_humi = humi
            
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

        elif msg_type == 20: # server_command ACK / stream_status
            action = data.get("action")
            if action == "stream_status":
                status = data.get("status", "inactive")
                url = data.get("stream_url", "")
                vehicle.is_streaming = 1 if status == "active" else 0
                vehicle.stream_url = url
                db.commit()
                print(f"[MQTT Gateway] Video stream status updated for {vid}: status={status}, url={url}")

        elif msg_type == 21: # vehicle_status_request
            driver = None
            if vehicle and vehicle.active_driver_id:
                driver = db.query(Driver).filter(Driver.driver_id == vehicle.active_driver_id).first()

            if not vehicle or not vehicle.active_driver_id:
                if vehicle:
                    vehicle.session_pending_confirm = 0
                reply_data = {"session_state": 0} # NONE
            else:
                vehicle.session_pending_confirm = 1
                driver_full_name = driver.full_name if driver else "Driver"
                reply_data = {
                    "session_state": 1, # PENDING_CONFIRM (Awaiting Admin Agree/Disagree)
                    "driver_id": vehicle.active_driver_id,
                    "driver_full_name": driver_full_name,
                    "existing_driver_id": vehicle.active_driver_id,
                    "existing_driver_name": driver_full_name
                }

            db.commit()

            now_ts = int(time.time())
            log21 = EventLog(vehicle_id=vid, timestamp=now_ts, type=21, details=json.dumps({"vid": vid}))
            log22 = EventLog(vehicle_id=vid, timestamp=now_ts, type=22, details=json.dumps(reply_data))
            db.add(log21)
            db.add(log22)
            db.commit()

            self.send_signed_reply(db, vid, 22, "vehicle/status/response", reply_data)
            print(f"[MQTT Gateway] Responding to vehicle_status_request (type 21) for {vid}: session_state={reply_data.get('session_state')}. Awaiting Admin Agree/Disagree.")

        db.commit()

    def send_signed_reply(self, db, vid: str, msg_type: int, sub_topic: str, data: dict):
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
        print(f"[MQTT Gateway] Sent signed reply type {msg_type} on {reply_topic}")
