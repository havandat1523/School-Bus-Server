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
    rfid_code = Column(String(50), unique=True, index=True)

class Student(Base):
    __tablename__ = "students"
    
    student_id = Column(Integer, primary_key=True, index=True)
    rfid_code = Column(String(50), unique=True, index=True)
    full_name = Column(String(100), nullable=False)
    parent_phone = Column(String(20))
    address = Column(String(200))
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
