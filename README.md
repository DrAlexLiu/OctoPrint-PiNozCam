# OctoPrint-PiNozCam
![Failure_Detection](/assets/images/failure_detection1.jpg)
## Introduction

Failure Detection Performed on your Pi CPU

Elevate your 3D printing experience with PiNozCam, a **FREE** and **No Subscription/Email Registration** plugin that brings **AI-powered monitoring** directly to your Raspberry Pi. **Local AI Processing** will protect your privacy and 3D model copyright. Stay informed with instant Failure **notifications** directly to your **Telegram** on **cellphone**. Download it and PiNozCam will offers peace of mind forever for free. 

**Features include:**

- **Local AI-Powered Monitoring**
- **Configurable Actions for Pause/Stop**
- **Telegram Remote Spaghetti Error Notification**
- **Performance Optimization On Pi Arm CPU**
- **User-Friendly Interface**

## Setup

### Hardware

**A Raspberry Pi with Fan**

We recommand a **RPi5** with Fan cooling. You can use Octoprint_deploy to install the octoprint and then install PiNozCam. If Linux is impossilbe for you, a RPi4 with octopi created by RPi Image creator also work. An old PC with octoprint installtion will do the trick as well. 

Performance:

- Raspberry Pi 5(x64, >=4GB): 1.7s/image
- Raspberry Pi 4(x32, >=4GB) : 6.7s/image
- PC with i5-10600K (x64): 0.4s/image

Fan cooling is strongly recommanded.

The AI can run most CPUs, including Arm. PiNozCam can even run on RPi3 and PiZero W 2, but consider their long inference time, we don't recommand you use these boards.

**An endoscope camera**

The plugin is compatible with most endoscope cameras on the market; however, ensure your camera operates at a frequency of 30Hz for optimal performance, supports a resolution of 480P or higher, and has built-in lighting. The distance between camera and nozzle are around 7 cm. 

Make sure your camera is clear, because some dust will make the camera dirty and impact the AI. 

### Software

Before jumping each parameter, let me introduct the workflow:

Detect Failure by AI
Confirm the size of failure on the image
Confirm the frequency of failure Detection
Action to perform




### Installation

You can install the PiNozCam plugin directly through OctoPrint's Plugin Manager by searching for "PiNozCam" in the repository. 

After installation, you can configure PiNozCam via the plugin settings in OctoPrint. Key configuration options include:

- **Action**: Choose the action PiNozCam should take upon detecting a print failure (e.g., notify, pause print, stop print).
- **AI Sensitivity**: Adjust the sensitivity of the AI detection algorithms to suit your printing environment and preferences.
- **CPU Speed Control**: Control the plugin's CPU usage, especially useful for Raspberry Pi users to balance performance and resource utilization.
- **Telegram Bot Token & Chat ID**: Enter your Telegram bot token and chat ID to enable instant notifications.

## Getting Started

Once installed and configured, PiNozCam will automatically start monitoring your prints using your printer's camera feed. You'll receive notifications based on your configured actions and can adjust settings anytime to fine-tune the monitoring performance.

Enjoy a new level of confidence in your 3D printing with OctoPrint-PiNozCam, your AI-powered watchful eye.


