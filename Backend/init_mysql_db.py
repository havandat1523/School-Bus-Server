import pymysql
import json
import time
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

mysql_host = "localhost"
mysql_user = "root"
mysql_pass = "123456"
db_name = "schoolbus"

try:
    print(f"[Init DB] Connecting to MySQL server at {mysql_host}...")
    conn = pymysql.connect(host=mysql_host, user=mysql_user, password=mysql_pass)
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
    conn.commit()
    cursor.close()
    conn.close()
    print(f"[Init DB] Database '{db_name}' verified/created.")
except Exception as e:
    print(f"[Init DB ERROR] Failed to connect or create MySQL database: {e}")
    exit(1)

DATABASE_URL = f"mysql+pymysql://{mysql_user}:{mysql_pass}@{mysql_host}:3306/{db_name}"
engine = create_engine(DATABASE_URL)

from models import Base, Vehicle, Driver, Attendant, Student, Manager, TripSession, RouteStop, StudentAbsence, School

print("[Init DB] Verifying database tables (preserving existing data)...")
# Base.metadata.drop_all(bind=engine) # DO NOT DROP EXISTING DATA
Base.metadata.create_all(bind=engine)
print("[Init DB] Schema verified.")

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
db = SessionLocal()

try:
    # A. Seed Vehicle if not exists
    vehicle_id = "29B-123.45"
    if not db.query(Vehicle).filter(Vehicle.id == vehicle_id).first():
        vehicle = Vehicle(
            id=vehicle_id,
            device_psk="b7e2f1a9c4d8e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0",
            chassis_number="RE123456789"
        )
        db.add(vehicle)
        print(f"[Init DB] Seeded vehicle: {vehicle_id}")
    
    # B. Seed Active Driver if not exists
    driver_id = "GV0012"
    if not db.query(Driver).filter(Driver.driver_id == driver_id).first():
        dummy_vector = [round(float(0.005 * i), 4) for i in range(128)]
        driver = Driver(
            driver_id=driver_id,
            full_name="Nguyen Van A",
            citizen_id="037123456789",
            license_class="B2",
            license_number="123456789",
            phone="0912345678",
            email="driver.a@schoolbus.com",
            status="active",
            face_vector_json=json.dumps(dummy_vector)
        )
        db.add(driver)
        print(f"[Init DB] Seeded active driver: {driver_id}")

    # C. Seed Attendant if not exists
    attendant_id = "PX0003"
    if not db.query(Attendant).filter(Attendant.attendant_id == attendant_id).first():
        attendant = Attendant(
            attendant_id=attendant_id,
            full_name="Le Thi B",
            rfid_code="11223344"
        )
        db.add(attendant)
        print(f"[Init DB] Seeded attendant: {attendant_id}")

    # D. Seed Student if not exists
    student_rfid = "04A3F1B2"
    if not db.query(Student).filter(Student.rfid_code == student_rfid).first():
        student = Student(
            rfid_code=student_rfid,
            full_name="Tran Van C",
            parent_phone="0987654321",
            address="123 Duong Lang, Ha Noi",
            student_class="3B",
            status="không ở trên xe",
            current_seat=0
        )
        db.add(student)
        print(f"[Init DB] Seeded student: {student_rfid}")
    
    # E. Seed Manager if not exists
    if not db.query(Manager).filter(Manager.username == "manager1").first():
        manager = Manager(
            username="manager1",
            password="01052003"
        )
        db.add(manager)
        print("[Init DB] Seeded manager profile: manager1 / 01052003")

    # F. Seed School if not exists
    if not db.query(School).first():
        school = School(
            name="Học Viện Kỹ Thuật Mật Mã",
            address="141 Chiến Thắng, Tân Triều, Thanh Trì, Hà Nội",
            lat=20.9806,
            lon=105.7958
        )
        db.add(school)
        print("[Init DB] Seeded default school: Hoc Vien Ky Thuat Mat Ma")
        
    db.commit()
    print("[Init DB] Seed data initialization completed successfully!")

except Exception as e:
    db.rollback()
    print(f"[Init DB ERROR] Failed to seed data: {e}")
finally:
    db.close()
