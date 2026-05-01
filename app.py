import RPi.GPIO as GPIO
from gpiozero import OutputDevice
import time
import cv2
import threading
import math
import numpy as np
from functools import wraps
from picamera2 import Picamera2
from flask import Flask, render_template, jsonify, request, Response

app = Flask(__name__)

# Basic login so random people on the wifi can't trigger the water gun
USER_CREDENTIALS = {"admin": "cyber2026"}

def check_auth(username, password):
    return username in USER_CREDENTIALS and USER_CREDENTIALS[username] == password

def authenticate():
    return Response(
        'Nope. Need the password to access this.', 401,
        {'WWW-Authenticate': 'Basic realm="Project Kai"'})

def requires_auth(f):
    # Wrapper to protect our web routes
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


class FireScanner:
    def __init__(self):
        # Hardware pins
        self.IR_LEFT = 19
        self.IR_CENTER = 21
        self.IR_RIGHT = 18
        self.SERVO_PAN = 24  
        self.SERVO_TILT = 25 
        
        # Keep track of what the system is doing
        self.auto_mode = False 
        self.sensor_state = {"LEFT": True, "CENTER": True, "RIGHT": True}
        self.current_frame = None
        self.hardware_lock = threading.Lock() # Prevents the camera from crashing if the web and the code pull at the same time

        # Servo and tracking variables
        self.current_pan = 7.5
        self.current_tilt = 7.5
        self.smooth_cx = 200
        self.smooth_cy = 150
        self.target_memory_frames = 0
        self.last_known_box = None

        # The color filter (HSV). We are looking for super bright, intense light.
        # This completely ignores dull yellow stuff like the floor.
        self.lower_fire_hsv = np.array([0, 80, 220]) 
        self.upper_fire_hsv = np.array([30, 255, 255])

        print("Warming up the camera...")
        self.picam2 = Picamera2()
        config = self.picam2.create_still_configuration(main={"size": (400, 300)})
        self.picam2.configure(config)
        self.picam2.start()
        time.sleep(2) # Give it a sec to adjust to the room lighting
        
        print("Setting up servos and the water pump...")
        self.pump = OutputDevice(16)
        self.pump.off()

        GPIO.setmode(GPIO.BCM)
        GPIO.setwarnings(False) 
        GPIO.setup([self.IR_LEFT, self.IR_CENTER, self.IR_RIGHT], GPIO.IN)
        GPIO.setup([self.SERVO_PAN, self.SERVO_TILT], GPIO.OUT)
        
        self.pwm_pan = GPIO.PWM(self.SERVO_PAN, 50)   
        self.pwm_tilt = GPIO.PWM(self.SERVO_TILT, 50)
        self.pwm_pan.start(0)
        self.pwm_tilt.start(0)
        
        self.center_servos()
        print("System is ready. Looking for fires.")

    def center_servos(self):
        # Snap back to the middle and cut power so they don't buzz and overheat
        self.pwm_pan.ChangeDutyCycle(7.5)
        self.pwm_tilt.ChangeDutyCycle(7.5)
        self.current_pan = 7.5
        self.current_tilt = 7.5
        time.sleep(0.5)
        self.pwm_pan.ChangeDutyCycle(0)
        self.pwm_tilt.ChangeDutyCycle(0)

    def aim_camera(self, zone=None, tilt_duty=None, raw_pan=None):
        # Moves the camera smoothly instead of whipping it around
        with self.hardware_lock:
            target_pan = raw_pan if raw_pan is not None else 7.5
            if zone == "LEFT": target_pan = 10.5 
            elif zone == "RIGHT": target_pan = 4.5  
            target_tilt = tilt_duty if tilt_duty is not None else 7.5
            
            step = 0.15  
            delay = 0.015 

            while abs(self.current_pan - target_pan) > step:
                self.current_pan += step if self.current_pan < target_pan else -step
                self.pwm_pan.ChangeDutyCycle(self.current_pan)
                time.sleep(delay)
            self.current_pan = target_pan
            self.pwm_pan.ChangeDutyCycle(self.current_pan)

            while abs(self.current_tilt - target_tilt) > step:
                self.current_tilt += step if self.current_tilt < target_tilt else -step
                self.pwm_tilt.ChangeDutyCycle(self.current_tilt)
                time.sleep(delay)
            self.current_tilt = target_tilt
            self.pwm_tilt.ChangeDutyCycle(self.current_tilt)

            time.sleep(0.2) 
            self.pwm_pan.ChangeDutyCycle(0)
            self.pwm_tilt.ChangeDutyCycle(0)

    def get_raw_frame(self):
        # Grab a picture, fix the colors, and flip it right-side up
        with self.hardware_lock:
            frame = self.picam2.capture_array()
            if frame is not None:
                frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                frame = cv2.flip(frame, -1)
            return frame

    def find_thermal_blobs(self, frame):
        # Swapped out the AI for pure math. Finds bright, hot stuff.
        hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        blurred = cv2.GaussianBlur(hsv_frame, (9, 9), 0) # blur to remove static
        
        mask = cv2.inRange(blurred, self.lower_fire_hsv, self.upper_fire_hsv)
        mask = cv2.erode(mask, None, iterations=2)
        mask = cv2.dilate(mask, None, iterations=2)
        
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        verified_fires = []
        for c in contours:
            # Filter out tiny glitches and massive floor reflections
            if 20 < cv2.contourArea(c) < 3000:
                x, y, w, h = cv2.boundingRect(c)
                verified_fires.append((x, y, w, h))
                
        return verified_fires

    def set_web_frame(self, frame, target_box=None, is_memory=False):
        # Draws the crosshairs and target boxes for the web stream
        if frame is not None:
            display_frame = frame.copy()
            cv2.line(display_frame, (190, 150), (210, 150), (0, 255, 0), 2)
            cv2.line(display_frame, (200, 140), (200, 160), (0, 255, 0), 2)
            
            if target_box is not None:
                x, y, w, h = target_box
                box_color = (0, 165, 255) if is_memory else (0, 0, 255)
                
                # Offsets so the right-mounted tube actually hits the fire
                TARGET_OFFSET_X = -35 
                TARGET_OFFSET_Y = 30  
                
                target_cx = max(0, min(399, x + w//2 + TARGET_OFFSET_X))
                target_base_y = max(0, min(295, y + h + TARGET_OFFSET_Y)) 
                
                cv2.rectangle(display_frame, (x, y), (x+w, y+h), box_color, 2)
                cv2.arrowedLine(display_frame, (200, 150), (target_cx, target_base_y), (0, 255, 0), 2, tipLength=0.1)
                cv2.circle(display_frame, (target_cx, target_base_y), 4, (0, 255, 255), -1) 

            # compress it to a jpeg so the website doesn't lag
            _, buffer = cv2.imencode('.jpg', display_frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            self.current_frame = buffer.tobytes()

    def capture_and_verify(self, zone):
        # An IR sensor tripped, let's see if it's actually a fire
        print(f"Heat detected on the {zone} sensor. Taking a look...")
        for tilt in [7.5, 6.0, 4.5]: 
            self.aim_camera(zone, tilt_duty=tilt)
            time.sleep(0.6) # Let the camera adjust to the light
            
            frame = self.get_raw_frame() 
            if frame is not None:
                verified_fires = self.find_thermal_blobs(frame)
                
                if len(verified_fires) > 0:
                    largest_fire = max(verified_fires, key=lambda item: item[2] * item[3])
                    self.set_web_frame(frame, largest_fire)
                    print(f"Found a fire in the {zone} zone!")
                    return True
                else:
                    self.set_web_frame(frame)
        self.center_servos()
        return False

    def mitigate_fire(self):
        print("Aiming and firing!")
        
        last_heat = time.time()
        pump_active = False 
        
        KP_PAN = 0.005
        KP_TILT = 0.005
        
        # Need to keep these identical to the ones in set_web_frame
        TARGET_OFFSET_X = -35 
        TARGET_OFFSET_Y = 30  
        
        while True:
            frame = self.get_raw_frame() 
            heat_found = False
            
            if any(GPIO.input(pin) == 0 for pin in [self.IR_LEFT, self.IR_CENTER, self.IR_RIGHT]):
                heat_found = True

            if frame is not None:
                verified_fires = self.find_thermal_blobs(frame)
                
                active_target = None
                is_memory = False
                
                if len(verified_fires) > 0:
                    heat_found = True 
                    self.target_memory_frames = 15 # Remember where it was for a split second if it flickers
                    active_target = max(verified_fires, key=lambda item: item[2] * item[3])
                    self.last_known_box = active_target
                    
                elif self.target_memory_frames > 0:
                    self.target_memory_frames -= 1
                    active_target = self.last_known_box
                    is_memory = True
                
                if active_target is not None:
                    self.set_web_frame(frame, active_target, is_memory) 
                    
                    x, y, w, h = active_target
                    raw_cx = max(0, min(399, x + (w // 2) + TARGET_OFFSET_X))
                    raw_base_y = max(0, min(295, y + h + TARGET_OFFSET_Y))
                    
                    # Smooth out the movement so the servos don't jitter
                    self.smooth_cx = int((0.85 * self.smooth_cx) + (0.15 * raw_cx))
                    self.smooth_cy = int((0.85 * self.smooth_cy) + (0.15 * raw_base_y))
                    
                    error_x = self.smooth_cx - 200
                    error_y = self.smooth_cy - 150
                    
                    # Trigger discipline: Only shoot water if we are perfectly aimed
                    if abs(error_x) <= 20 and abs(error_y) <= 20:
                        if not pump_active:
                            self.pump.on()
                            print("Lined up. Shooting water!")
                            pump_active = True
                            
                        with self.hardware_lock:
                            self.pwm_pan.ChangeDutyCycle(0) 
                            self.pwm_tilt.ChangeDutyCycle(0)
                            
                    else:
                        if pump_active:
                            self.pump.off()
                            print("Target moved. Pausing water to re-aim.")
                            pump_active = False
                            
                        new_pan = self.current_pan + (error_x * KP_PAN)
                        new_tilt = self.current_tilt - (error_y * KP_TILT)
                        
                        new_pan = max(4.5, min(10.5, new_pan))
                        new_tilt = max(2.5, min(7.5, new_tilt)) 
                        
                        with self.hardware_lock:
                            move_required = False
                            if abs(new_pan - self.current_pan) > 0.15: # Deadband to prevent buzzing
                                self.pwm_pan.ChangeDutyCycle(new_pan)
                                self.current_pan = new_pan
                                move_required = True
                                
                            if abs(new_tilt - self.current_tilt) > 0.15:
                                self.pwm_tilt.ChangeDutyCycle(new_tilt)
                                self.current_tilt = new_tilt
                                move_required = True
                                
                            if move_required:
                                time.sleep(0.04) 
                            else:
                                self.pwm_pan.ChangeDutyCycle(0) 
                                self.pwm_tilt.ChangeDutyCycle(0)

                else:
                    if pump_active:
                        self.pump.off()
                        print("Lost it. Holding position.")
                        pump_active = False
                        
                    self.set_web_frame(frame) 
                    
                    with self.hardware_lock:
                        self.pwm_pan.ChangeDutyCycle(0)
                        self.pwm_tilt.ChangeDutyCycle(0)

            if heat_found:
                last_heat = time.time()
            else:
                # If everything has been clear for 10 seconds, go back to sleep
                if (time.time() - last_heat) >= 10:
                    print("Fire looks dead. Returning to base.")
                    self.pump.off()
                    self.center_servos() 
                    break
            
            time.sleep(0.02) 

    def run_scan(self):
        # Runs in the background constantly checking the sensors
        print("Scanner running. Waiting for something to happen.")
        while True:
            try:
                frame = self.get_raw_frame()
                self.set_web_frame(frame) 
                
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
                # Keep the script from crashing if something random goes wrong
                print(f"Threw an error, but kept going: {e}")
                time.sleep(1) 

# --- FLASK ROUTES ---
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
