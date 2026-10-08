import time
import os
import logging
from mqtt_listener import ServerMQTTListener
from database import Base, engine

# Configure logging to write to gateway.log and console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(__file__), "gateway.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Initialize database models on startup
logging.info("[Gateway Server] Initializing database models...")
Base.metadata.create_all(bind=engine)

def main():
    logging.info("[Gateway Server] Starting standalone MQTT receiver gateway...")
    listener = ServerMQTTListener()
    listener.start()
    
    # Keep the main thread alive to handle keyboard interrupts
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("[Gateway Server] Stopping receiver...")
        listener.running = False

if __name__ == "__main__":
    main()
