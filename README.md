# OctoPrint-PiNozCam
![Failure_Detection](/assets/images/failure_detection1.jpg)
## Introduction

Failure Detection Performed on your Pi CPU

Unlock advanced 3D printing monitoring with PiNozCam, your go-to solution for **AI-powered surveillance** — all **without a subscription or email registration**. Designed to enhance your printing process, PiNozCam introduces cutting-edge **edge computing** right on your Raspberry Pi. This ensures your **privacy** and the protection of your 3D model **copyrights**, while enabling you to receive instant failure alerts directly on your mobile via **Telegram**. Best of all, PiNozCam is entirely free, offering you peace of mind at no extra cost. 

**Features include:**

- **AI-Powered Edge Computing for Monitoring**
- **Configurable Actions for Pause/Stop**
- **Instant Telegram Error Notifications**
- **Performance Optimization On Pi Arm CPU**
- **User-Friendly Interface**
- **Privacy First, No email register/sign up/subscription**

Download PiNozCam today and enjoy uninterrupted, worry-free 3D printing forever.

## Setup

### Hardware Setup

#### **Raspberry Pi with Cooling Fan**

- Raspberry Pi 5(x64, >=4GB): 35.29 images / minute (Highly Recommand)
  
  Use Octoprint_deploy to install the octoprint and then install PiNozCam
- Raspberry Pi 4B(x32, >=4GB) : 8.96 images / minute (Recommand)
  
  Use RPi Imager to flash OctoPi and install the PiNozCam
- Old PC like i5-10600K (x64): 150 images / minute
  
  Use Octoprint docker and install the PiNozCam, if you use old PC to control printer

We strongly recommend **fan cooling** to maintain optimal performance. The system is compatible with most CPUs, including ARM architecture. Although PiNozCam can run on Raspberry Pi 3 and PiZero W 2, their longer inference times make them less recommended options. 

#### **Endoscope Camera**

Most market-available endoscope cameras are compatible with this setup. Ensure your camera:
- Operates at a 30Hz frequency to minimize motion blur.
- Supports a minimum resolution of 480P.
- Features built-in lighting for enhanced detection quality.
- Is positioned approximately 7 cm from the nozzle. 

Cleaning the camera before each print is crucial as dust can accumulate and affect detection accuracy.


### **Software Configuration**

Screenshot:

![screenShot](/assets/images/screenshot.png)

<img src="/assets/images/screenshot.png" width="400" height="400">


**Key Parameters:**

- **Action:** Specifies the action PiNozCam should take when a print failure is detected (e.g., notify only, pause print, stop print). Detected failures are displayed in the video stream for 5 seconds, allowing for immediate visual verification.
- **Image Sensitivity:** Adjust the sensitivity to ensure accurate detection of print failures. Set the threshold to balance between premature stopping for minor issues and delaying action for significant errors. A starting value of 0.08 or 8% is recommended for optimal balance.
- **Failure Scores Threshold:** Define the confidence level at which an anomaly is considered a print failure. This setting helps in reducing false alarms by setting a minimum probability threshold for errors, ensuring that only genuine failures prompt action.
- **Max Failure Count:** Specify the number of detections required in **Failure Consider Time** before PiNozCam takes the configured action. A value above 1 is recommended to avoid false positives.
- **Failure Consider Time (s):** Implement a time buffer to focus on recent failures, ignoring older detections that may no longer be relevant. This dynamic consideration helps in adapting to the current state of the print.
- **CPU Speed Control:** Offers options for running the CPU at half or full speed. Half speed is recommended in warmer conditions without adequate cooling to prevent overheating. Full speed is optimal with enforced cooling.

For notifications, enter your Telegram bot token and chat ID.

Initially, stick with the default settings and adjust them gradually to fine-tune performance.




## Final Step: Start Printing

Once everything is set up, you can relax and rely on Telegram notifications to alert you of any issues during printing.

