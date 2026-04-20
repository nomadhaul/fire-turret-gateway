import RPi.GPIO as GPIO
from gpiozero import OutputDevice
import time
import cv2
import threading
from functools import wraps
from picamera2 import Picamera2
from flask import Flask, render_template, jsonify, request, Response

app = Flask(__name__)

# --- CYBERSECURITY LOGIC (HTTP BASIC AUTH) ---
USER_CREDENTIALS = {
    "admin": "cyber2026"
}

def check_auth(username, password):
    return username in USER_CREDENTIALS and USER_CREDENTIALS[username] == password

def authenticate():
    return Response(
        'ACCESS DENIED: Please enter valid credentials.', 401,
        {'WWW-Authenticate': 'Basic realm="Turret Command Center"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated
# ---------------------------------------------

class FireScanner:
    def __init__(self):
        self.IR_LEFT = 19
        self.IR_CENTER = 21
        self.IR_RIGHT = 18
        self.SERVO_PAN = 24  
        self.SERVO_TILT = 25 
        
        self.auto_mode = False 
        self.sensor_state = {"LEFT": True, "CENTER": True, "RIGHT": True}
        self.current_frame = None
        self.hardware_lock = threading.Lock() 

        # --- Tracker for Smooth Movements ---
        self.current_pan = 7.5
        self.current_tilt = 7.5

        print("Initializing Camera Stack...")
        self.picam2 = Picamera2()
        config = self.picam2.create_still_configuration(main={"size": (400, 300)})
        self.picam2.configure(config)
        self.picam2.start()
        time.sleep(2) 
        
        print("Loading AI Model...")
        self.fire_cascade = cv2.CascadeClassifier('fire_detection.xml')

        self.pump = OutputDevice(16)
        self.pump.off()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False) 
        GPIO.setup([self.IR_LEFT, self.IR_CENTER, self.IR_RIGHT], GPIO.IN)
        
        print("Initializing Servos...")
        GPIO.setup([self.SERVO_PAN, self.SERVO_TILT], GPIO.OUT)
        self.pwm_pan = GPIO.PWM(self.SERVO_PAN, 50)   
        self.pwm_tilt = GPIO.PWM(self.SERVO_TILT, 50)
        self.pwm_pan.start(0)
        self.pwm_tilt.start(0)
        
        # Initialize at center
        self.pwm_pan.ChangeDutyCycle(self.current_pan)
        self.pwm_tilt.ChangeDutyCycle(self.current_tilt)
        time.sleep(0.5)
        self.pwm_pan.ChangeDutyCycle(0)
        self.pwm_tilt.ChangeDutyCycle(0)

    def aim_camera(self, zone=None, tilt_duty=None, raw_pan=None):
        """Smoothly sweeps servos to the target location."""
        with self.hardware_lock:
            if raw_pan is not None: 
                target_pan = raw_pan
            else:
                if zone == "CENTER": target_pan = 7.5  
                elif zone == "LEFT": target_pan = 11.0 
                elif zone == "RIGHT": target_pan = 4.0  
                else: target_pan = 7.5
                
            if tilt_duty is None: 
                target_tilt = 7.5
            else:
                target_tilt = tilt_duty

            step = 0.15  
            delay = 0.015 

            while abs(self.current_pan - target_pan) > step:
                if self.current_pan < target_pan: self.current_pan += step
                else: self.current_pan -= step
                self.pwm_pan.ChangeDutyCycle(self.current_pan)
                time.sleep(delay)
            self.current_pan = target_pan
            self.pwm_pan.ChangeDutyCycle(self.current_pan)

            while abs(self.current_tilt - target_tilt) > step:
                if self.current_tilt < target_tilt: self.current_tilt += step
                else: self.current_tilt -= step
                self.pwm_tilt.ChangeDutyCycle(self.current_tilt)
                time.sleep(delay)
            self.current_tilt = target_tilt
            self.pwm_tilt.ChangeDutyCycle(self.current_tilt)

            time.sleep(0.2) 
            self.pwm_pan.ChangeDutyCycle(0)
            self.pwm_tilt.ChangeDutyCycle(0)

    def update_frame(self):
        with self.hardware_lock:
            frame = self.picam2.capture_array()
        if frame is not None:
            _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            self.current_frame = buffer.tobytes()
            return frame
        return None

    def capture_and_verify(self, zone):
        print(f"\n[{zone}] IR Triggered! Scanning...")
        for tilt in [9.0, 7.5, 6.0]:
            self.aim_camera(zone, tilt_duty=tilt)
            frame = self.update_frame() 
            if frame is not None:
                gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                fires = self.fire_cascade.detectMultiScale(gray_frame, scaleFactor=1.2, minNeighbors=5)
                if len(fires) > 0:
                    print(f"🚨 FIRE DETECTED in {zone} at elevation {tilt}!")
                    return True
        self.aim_camera("CENTER", 7.5) 
        return False

    def mitigate_fire(self):
        print("\n🔥 MITIGATION ACTIVE 🔥")
        self.pump.on()
        last_heat = time.time()
        while True:
            self.update_frame() 
            heat_found = False
            if self.sensor_state["CENTER"] and GPIO.input(self.IR_CENTER) == 0: heat_found = True
            if self.sensor_state["LEFT"] and GPIO.input(self.IR_LEFT) == 0: heat_found = True
            if self.sensor_state["RIGHT"] and GPIO.input(self.IR_RIGHT) == 0: heat_found = True

            if heat_found:
                last_heat = time.time()
                time.sleep(0.5) 
                continue
            
            if (10 - (time.time() - last_heat)) <= 0:
                print("\n✅ Area clear. Pump off.")
                self.pump.off()
                self.aim_camera("CENTER", 7.5) 
                break
            time.sleep(0.5) 

    def run_scan(self):
        print("\nSystem Armed. Awaiting connections.")
        try:
            while True:
                self.update_frame() 
                if self.auto_mode:
                    if self.sensor_state["CENTER"] and GPIO.input(self.IR_CENTER) == 0:
                        if self.capture_and_verify("CENTER"): self.mitigate_fire()
                        time.sleep(2) 
                    elif self.sensor_state["LEFT"] and GPIO.input(self.IR_LEFT) == 0:
                        if self.capture_and_verify("LEFT"): self.mitigate_fire()
                        time.sleep(2)
                    elif self.sensor_state["RIGHT"] and GPIO.input(self.IR_RIGHT) == 0:
                        if self.capture_and_verify("RIGHT"): self.mitigate_fire()
                        time.sleep(2)
                time.sleep(0.05) 
        except Exception as e:
            print(f"Error: {e}")

# --- Web Endpoints ---
scanner = FireScanner()

@app.route('/')
@requires_auth  
def index():
    return render_template('index.html')

@app.route('/video_feed')
@requires_auth 
def video_feed():
    def generate():
        while True:
            if scanner.current_frame:
                yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + scanner.current_frame + b'\r\n')
            time.sleep(0.05)
    return Response(generate(), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/mode/<mode>', methods=['POST'])
@requires_auth
def set_mode(mode):
    scanner.auto_mode = (mode == 'auto')
    return jsonify(mode=mode)

@app.route('/api/camera/move', methods=['POST'])
@requires_auth
def move_camera():
    pan = request.args.get('pan')
    tilt = request.args.get('tilt')
    p_duty = float(pan) if pan != 'keep' else None
    t_duty = float(tilt) if tilt != 'keep' else None
    scanner.aim_camera(raw_pan=p_duty, tilt_duty=t_duty)
    return jsonify(status="ok")

@app.route('/api/pump/<state>', methods=['POST'])
@requires_auth
def control_pump(state):
    if state == 'on': scanner.pump.on()
    else: scanner.pump.off()
    return jsonify(status="ok")

@app.route('/api/sensor/<zone>/toggle', methods=['POST'])
@requires_auth
def toggle_sensor(zone):
    scanner.sensor_state[zone] = not scanner.sensor_state[zone]
    return jsonify(state=scanner.sensor_state[zone])

if __name__ == '__main__':
    threading.Thread(target=scanner.run_scan, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, threaded=True)
