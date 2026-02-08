import time
import board
import imufusion
import numpy as np
from adafruit_lsm6ds.lsm6dsox import LSM6DSOX
from adafruit_lis3mdl import LIS3MDL

# Initialize I2C and Sensors
i2c = board.I2C() 
sox = LSM6DSOX(i2c)
lis3 = LIS3MDL(i2c)

# Initialize the AHRS Filter
ahrs = imufusion.Ahrs()

print("--- CubeSat IMU Fusion Started ---")
print("Press Ctrl+C to stop and exit.")

last_time = time.monotonic()

try:
    while True:
        # Calculate precise delta time (dt) for the filter
        current_time = time.monotonic()
        dt = current_time - last_time
        last_time = current_time

        # 3. Read Raw Data
        # Accel: m/s^2
        accel = np.array(sox.acceleration) 
        # Gyro: Convert rad/s to degrees/s
        gyro = np.array([d * 57.2958 for d in sox.gyro])
        # Mag: uT (microTeslas)
        mag = np.array(lis3.magnetic)

        # Update the Kalman-based filter
        # Arguments: Gyro (deg/s), Accel (m/s^2), Mag (uT), DeltaTime
        ahrs.update(gyro, accel, mag, dt)

        # Get Orientation (Euler Angles)
        # Returns a list: [roll, pitch, yaw] in degrees
        euler = ahrs.quaternion.to_euler()

        # Print Results
        # Using :6.1f for clean alignment in the terminal
        print(f"Roll: {euler[0]:6.1f} | Pitch: {euler[1]:6.1f} | Yaw: {euler[2]:6.1f}", end="\r")

        # Aim for ~100Hz frequency
        time.sleep(0.01)

except KeyboardInterrupt:
    print("\n\nScript stopped by user.")
