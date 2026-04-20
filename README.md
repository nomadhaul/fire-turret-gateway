# Edge-to-Cloud Automated Fire Mitigation System

An Industrial Control System (ICS) prototype that integrates localized hardware-in-the-loop (Raspberry Pi, IR sensors, PWM servos) with a multithreaded AI vision pipeline, securely bridged to a public AWS gateway via a reverse SSH tunnel.

## System Architecture

This project is divided into three core engineering pillars:

1. **Hardware & Edge Computing:** - Utilizes a Raspberry Pi running an object-oriented Python stack. 
   - Hardware interrupts from active-low Infrared sensors trigger autonomous hardware movements via PWM servos.
2. **AI Verification Pipeline:** - Implements a hardware lock (Mutex) to manage concurrent access to the PiCamera2 feed.
   - When heat is detected, the system executes a smooth vertical sweep algorithm, passing frames to an OpenCV Haar Cascade classifier to visually verify the presence of a fire signature before engaging the water pump relay.
3. **Secure Cloud Gateway:** - The local edge device establishes a persistent, encrypted reverse SSH tunnel to an AWS EC2 instance.
   - The Flask web dashboard is served over the public internet and secured via HTTP Basic Authentication to prevent unauthorized access by automated bot scanners.

## Tech Stack
* **Language:** Python 3 (OOP, Multithreading)
* **Computer Vision:** OpenCV, PiCamera2
* **Web Framework:** Flask
* **Hardware:** RPi.GPIO, gpiozero, PWM Servos, IR Sensors, 5V Relay
* **Cloud & Networking:** AWS EC2 (Ubuntu), Reverse SSH Tunneling

## Security Features
* **Zero-Trust Local Execution:** The system defaults to manual mode and requires explicit user authentication before any hardware controls are exposed.
* **HTTP Basic Authentication:** Protects the cloud-facing web interface and video stream.
* **Resource Locking:** Thread-safe hardware locks prevent race conditions between the Flask web server and the autonomous background monitoring daemon.

## Usage
*(Note: This project requires specific physical hardware wiring. Do not run this code without configuring the GPIO pins to match your physical layout).*

1. Clone the repository.
2. Install dependencies: `pip install -r requirements.txt`
3. Provide your own `fire_detection.xml` Haar Cascade model.
4. Run the edge server: `python app.py`
5. (Optional) Fire a reverse SSH tunnel to your cloud provider to expose the Flask port (5000) externally.
