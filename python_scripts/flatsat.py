"""
The Python code you will write for this module should read
acceleration data from the IMU. When a reading comes in that surpasses
an acceleration threshold (indicating a shake), your Pi should pause,
trigger the camera to take a picture, then save the image with a
descriptive filename. You may use GitHub to upload your images automatically,
but for this activity it is not required.

The provided functions are only for reference, you do not need to use them. 
You will need to complete the take_photo() function and configure the VARIABLES section
"""
#l;jksdg;dsfgds;gjed;ogvnjerol;ihjnsdeogiukjdgo;nhjzxffdb,.nzdfkgzshdg
#AUTHOR: 
#DATE:

#import libraries
import time
import board
from adafruit_lsm6ds.lsm6dsox import LSM6DSOX as LSM6DS
from adafruit_lis3mdl import LIS3MDL
from git import Repo
from picamera2 import Picamera2

#VARIABLES
THRESHOLD = 15.0      #Standard gravity is ~9.8. A "shake" is usually anything over 15-20.
REPO_PATH = "/home/kaputnik"     #Your github repo path: ex. /home/pi/FlatSatChallenge
FOLDER_PATH = "/Pictures"   #Your image folder path in your GitHub repo: ex. /Images

#imu and camera initialization
i2c = board.I2C()
accel_gyro = LSM6DS(i2c)
mag = LIS3MDL(i2c)
picam2 = Picamera2()

def git_push():
    """
    This function is complete. Stages, commits, and pushes new images to your GitHub repo.
    """
    try:
        repo = Repo(REPO_PATH)
        origin = repo.remote('origin')
        print('added remote')
        origin.pull()
        print('pulled changes')
        repo.git.add(REPO_PATH + FOLDER_PATH)
        repo.index.commit('New Photo')
        print('made the commit')
        origin.push()
        print('pushed changes')
    except:
        print('Couldn\'t upload to git')


def img_gen(name):
    """
    This function is complete. Generates a new image name.

    Parameters:
        name (str): your name ex. MasonM
    """
    t = time.strftime("_%H%M%S")
    imgname = (f'{REPO_PATH}/{FOLDER_PATH}/{name}{t}.jpg')
    return imgname

def take_photo():
    print("CubeSat is active. Waiting for shake...")
    while True:
        # Assess acceleration data
        accelx, accely, accelz = accel_gyro.acceleration
        
        # Calculate the total acceleration magnitude (optional) or check individual axes
        # We check if any axis exceeds our THRESHOLD
        if abs(accelx) > THRESHOLD or abs(accely) > THRESHOLD or abs(accelz) > THRESHOLD:
            print("Shake detected! Taking picture...")
            
            # 1. PAUSE to let movement stabilize
            time.sleep(1)
            
            # 2 Generate filename
            name = "KAPUTNIK_image" 
            filename = img_gen(name)
            
            # 3. TAKE PHOTO
            picam2.start()
            time.sleep(2) # Sensor adjusts to light
            picam2.capture_file(filename)
            picam2.stop()
            print(f"Photo saved as {filename}")

            # 4. PUSH PHOTO TO GITHUB
            git_push()
            
            # 5. COOLDOWN PAUSE (Prevents taking 100 photos in one second)
            time.sleep(5)
        
        # Short pause to prevent maxing out the CPU
        time.sleep(0.1)

def main():
    take_photo()

if __name__ == '__main__':
    main()

#reconfiguring github