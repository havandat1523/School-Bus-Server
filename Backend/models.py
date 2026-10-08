from sqlalchemy import Column, Integer, String, Float, Text
from database import Base

class Vehicle(Base):
    __tablename__ = "vehicles"
    
    id = Column(String(50), primary_key=True, index=True) # Placa plate: e.g. 29B-123.45
    device_psk = Column(String(64), nullable=False)       # HMAC PSK Hex
    chassis_number = Column(String(50))
    active_driver_id = Column(String(50))
    active_attendant_id = Column(String(50))
    last_lat = Column(Float, default=21.0021)
    last_lon = Column(Float, default=105.8462)
    last_speed = Column(Float, default=0.0)
    students_onboard = Column(Integer, default=0)
    seats_bitmap = Column(Integer, default=0) # 16-bit seat map integer
    last_temp = Column(Float, default=0.0)
    last_humi = Column(Float, default=0.0)
    is_streaming = Column(Integer, default=0)
    stream_url = Column(String(200), default="")
    session_pending_confirm = Column(Integer, default=0) # 0=Normal, 1=Pending Admin Agree/Disagree
    sos_active = Column(Integer, default=0)              # 0=No SOS, 1=SOS Alert Triggered

class Driver(Base):
    __tablename__ = "drivers"
    
    driver_id = Column(String(50), primary_key=True, index=True) # e.g. GV0012
    full_name = Column(String(100), nullable=False)
    citizen_id = Column(String(50))
    license_class = Column(String(20))
    license_number = Column(String(50))
    phone = Column(String(20))
    email = Column(String(100))
    status = Column(String(20), default="pending") # active, pending, suspended
    face_vector_json = Column(Text) # JSON list of 128 floats

class Attendant(Base):
    __tablename__ = "attendants"
    
    attendant_id = Column(String(50), primary_key=True, index=True) # e.g. PX0003
    full_name = Column(String(100), nullable=False)
    citizen_id = Column(String(50))
    phone = Column(String(20))
    email = Column(String(100))
    rfid_code = Column(String(50), unique=True, index=True)
    status = Column(String(20), default="active") # active, suspended
    duty_status = Column(String(20), default="offduty") # offduty, onduty

class Student(Base):
    __tablename__ = "students"
    
    student_id = Column(Integer, primary_key=True, index=True)
    rfid_code = Column(String(50), unique=True, index=True)
    full_name = Column(String(100), nullable=False)
    parent_phone = Column(String(20))
    address = Column(String(200))
    lat = Column(Float, default=20.9806)
    lon = Column(Float, default=105.7958)
    student_class = Column(String(50))
    status = Column(String(20), default="active") # active, vacation, Đang trên xe, không ở trên xe
    current_seat = Column(Integer, default=0)

class Manager(Base):
    __tablename__ = "managers"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password = Column(String(100), nullable=False)

class TelemetryLog(Base):
    __tablename__ = "telemetry_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(String(50), index=True)
    timestamp = Column(Integer, nullable=False)
    lat = Column(Float)
    lon = Column(Float)
    speed = Column(Float)
    student_count = Column(Integer)
    temp = Column(Float)
    humi = Column(Float)

class EventLog(Base):
    __tablename__ = "event_logs"
    
    id = Column(Integer, primary_key=True, index=True)
    vehicle_id = Column(String(50), index=True)
    timestamp = Column(Integer, nullable=False)
    type = Column(Integer, nullable=False) # Envelope type
    details = Column(Text) # JSON string of event payload

class TripSession(Base):
    __tablename__ = "trip_sessions"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    vehicle_id = Column(String(50), index=True)
    route_id = Column(Integer, default=1)
    trip_date = Column(String(20)) # e.g. "2026-07-27"
    trip_shift = Column(String(20), default="MORNING") # MORNING / AFTERNOON
    trip_phase = Column(String(30), default="PICKUP") # PICKUP, ARRIVE_SCHOOL, DEPART_SCHOOL, DROPOFF, COMPLETED
    students_onboard = Column(Integer, default=0)
    boarded_student_ids = Column(Text, default="[]") # JSON list of student IDs
    session_state = Column(Integer, default=0) # 0=NONE, 1=PENDING_CONFIRM, 2=RESUMED, 3=REQUIRE_NEW_LOGIN
    driver_id = Column(String(50), nullable=True)
    attendant_id = Column(String(50), nullable=True)
    updated_at = Column(Integer, default=0)

class RouteStop(Base):
    __tablename__ = "RouteStop"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    route_id = Column(Integer, index=True)
    student_id = Column(Integer, nullable=True)
    stop_order = Column(Integer)
    address = Column(String(250))
    lat = Column(Float, default=0.0)
    lon = Column(Float, default=0.0)
    geometry = Column(Text) # JSON string of coordinates [[lon, lat], ...]
    distance_m = Column(Float, default=0.0)
    duration_s = Column(Float, default=0.0)

class School(Base):
    __tablename__ = "schools"
    
    school_id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), default="Trường Học Viện Kỹ Thuật Mật Mã")
    address = Column(String(250), default="141 Chiến Thắng, Tân Triều, Thanh Trì, Hà Nội")
    lat = Column(Float, default=20.9806)
    lon = Column(Float, default=105.7958)

class StudentAbsence(Base):
    __tablename__ = "student_absences"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    student_id = Column(Integer, index=True)
    absence_date = Column(String(20))
    shift = Column(String(20), default="MORNING")


# F1: Log kiểm toán điểm danh thủ công (học sinh & phụ xe quên thẻ)
class ManualAttendanceLog(Base):
    __tablename__ = "manual_attendance_logs"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    target_type   = Column(String(20), nullable=False)   # "student" | "attendant"
    target_id     = Column(String(50), nullable=False)   # student_id hoặc attendant_id
    vehicle_id    = Column(String(50), index=True)
    trip_session_id = Column(Integer, nullable=True)
    direction     = Column(String(10), nullable=False)   # "board" | "alight"
    seat_number   = Column(Integer, default=0)
    confirmed_by  = Column(String(50))                   # username admin
    reason        = Column(String(200))
    confirmed_at  = Column(Integer)                      # unix timestamp


# F3: Sự kiện cảnh báo khẩn cấp SOS
class EmergencyEvent(Base):
    __tablename__ = "emergency_events"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    sos_id       = Column(String(36), unique=True, index=True)
    vehicle_id   = Column(String(50), index=True)
    triggered_by = Column(Integer, default=1)   # 1=slave_button, 2=ui_button, 3=gpio_button
    lat          = Column(Float, default=0.0)
    lon          = Column(Float, default=0.0)
    status       = Column(String(20), default="RECEIVED")  # RECEIVED, ACKED, CANCELLED, RESOLVED
    created_at   = Column(Integer)                         # unix timestamp
    acked_at     = Column(Integer, nullable=True)
    acked_by     = Column(String(50), nullable=True)
