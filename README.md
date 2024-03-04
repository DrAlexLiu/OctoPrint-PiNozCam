# OctoPrint-PiNozCam
![Failure_Detection](/assets/images/failure_detection1.jpg)
## Introduction

Failure Detection Performed on Pi CPU

Elevate your 3D printing experience with PiNozCam, a **FREE** and **No Subscription** plugin that brings **AI-powered monitoring** directly to your Raspberry Pi. Process your print jobs **locally** with advanced AI algorithms that detect potential failures, ensuring **privacy** and no leak on **3D model copyright**. Stay informed with instant **notifications** sent directly to your **Telegram** on **cellphone**, keeping you connected to your prints anytime, anywhere. PiNozCam is the ultimate, FREE solution for optimizing your 3D printing process with the power of AI, right at your fingertips.

**Features include:**

- **Local AI-Powered Monitoring**
- **Configurable Actions for Pause/Stop**
- **Telegram Remote Notification**
- **Performance Optimization On Pi Arm CPU**
- **User-Friendly Interface**

Whether you're running a print farm or a single printer in your workshop, PiNozCam offers peace of mind and a new level of interaction with your 3D printing process.

**Tested Boards and Inference Time:**
- Raspberry Pi 3: (16s/image)
- Raspberry Pi 4: (6.7s/image)
- Raspberry Pi 5(x64): (1.7s/image)
- Octoprint Docker on PC (x64): (0.4s/image)

We recommand the RPi4 or RPi5 for fast analysis and quick response. Althought the inference would takes several seconds, it is still can provide reliable survilliance for the 3D printing progress.

## Setup

### Hardware
 
To use the PiNozCam plugin, you'll need a Raspberry Pi, a compatible endoscope camera with a 3D-printed mounting bracket, and a 3D printer. The plugin is compatible with most endoscope cameras on the market; however, ensure your camera has built-in lighting, supports a resolution of 480P or higher, and operates at a frequency of 30Hz for optimal performance.

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


