# OctoPrint-PiNozCam Plugin

## Introduction

The OctoPrint-PiNozCam plugin is a powerful enhancement for your 3D printing experience, leveraging artificial intelligence to monitor and identify potential print failures in real-time. Designed to integrate seamlessly with OctoPrint's ecosystem, PiNozCam provides an additional layer of reliability and efficiency to your 3D printing projects.

By analyzing images from your printer's camera, PiNozCam can detect anomalies that may indicate a print failure, such as warping, spaghetti-like extrusions, or detachment from the print bed. Upon detecting a potential failure, the plugin can take configurable actions, such as pausing the print, stopping it, or sending a notification through Telegram, allowing you to intervene before wasting materials or time.

**Features include:**

- **AI-Powered Monitoring**: Utilizes advanced image recognition algorithms to monitor your prints.
- **Configurable Actions**: Customize the plugin's response to detected print failures, including notifications or automatic print pausing or stopping.
- **Telegram Integration**: Receive instant alerts with images of the potential print failure directly on your phone.
- **Performance Optimization**: Designed to run efficiently on Raspberry Pi, with options to control CPU usage based on your setup.
- **User-Friendly Interface**: Easily manage settings and monitor AI analysis results through the OctoPrint interface.

Whether you're running a print farm or a single printer in your workshop, PiNozCam offers peace of mind and a new level of interaction with your 3D printing process.

## Setup

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


