# OctoPrint-PiNozCam
<div>
  <img src="/assets/images/failure_detection1.jpg" width="40%" height="40%">
  <img src="/assets/images/failure_detection2.jpg" width="48%" height="48%">
</div>

[![Join Discord](https://img.shields.io/discord/1158238902197424251.svg?label=Discord&logo=discord&logoColor=ffffff&color=7389D8&labelColor=555555)](https://discord.gg/gv4tKJ2ZKr)

## Introduction

Welcome to the era of edge computing with free failure detection performed directly on your device

**Device (50% of All Cores for AI)**|**Detect Speed (images / minute)**
:-----:|:-----:
Raspberry Pi 5|45
Raspberry Pi 4|9

<details>
  <summary>More Devices</summary>
  
  **Device (50% of All Cores for AI)**|**Detect Speed (images / minute)**
  :-----:|:-----:
  Raspberry Pi 3B|5 
  PC with Intel i5 10600|260
  OrangePi Zero 2/3|9
  Raspberry Pi Zero 2 W|3

  <sub>*The inference speed tests were conducted under the circumstance that 50% of the device's cores were allocated for AI processing, while the remaining 50% of the cores were dedicated to OctoPrint and printing processes.</sub>
</details>

Unlock advanced 3D printing monitoring with PiNozCam, your go-to solution for **AI-powered surveillance** — all **without any subscription or registration**. PiNozCam brings cutting-edge computing to your **Raspberry Pi** or any old PC/single board computer, ensuring **privacy** and providing instant failure alerts via **Telegram/Discord**. 

| | | |
|:--|:--|:--|
| **Fast Local Detection** | **Instant Notifications** | **Privacy-Focused** |
| **Auto Pause/Stop** | **No Subscriptions** | **Forever Free** |

<details>
<summary>Support Platforms</summary>
  Support RPi OS platform ([Don’t know❓](https://raspberrytips.com/which-raspberry-pi-os-is-running/)):

  **OS platform**|**Buster**|**Bullseye**|**Bookworm**
  :-----:|:-----:|:-----:|:-----:
  arm64 (x64)|✅|✅|✅
  armhf (x32)|✅|✅|✅

  ⚠️ This plugin supports the [OctoPi image](https://www.raspberrypi.com/tutorials/set-up-raspberry-pi-octoprint/) . However, I am still working on this plugin on [Octo4a](https://github.com/feelfreelinux/octo4a) and it may be supported in the future versions. 

  **RPi(Boardcom)**|**Intel/AMD CPU**|**AllWinner**|**RockChip**|**RAM**
  :-----:|:-----:|:-----:|:-----:|:-----:
  ✅|✅|✅|✅|>=512MB
</details>

<details>
<summary>Plugin Setup</summary>

## Plugin Setup

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/master/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/master.zip
    
</details>

## Plugin Setting

| **Choose Mode:** | |
|:--|:--|
| **NozzleCam** | **WebCam** |
| <img src="/assets/images/nozzle_cam_setup.jpg" width=50% height=50%> | <img src="/assets/images/nozzle_cam_setup.jpg" width=50% height=50%> |
| Boxes Display Threshold:0.6<br>Image Sensitivity：>0.05<br>Failure Scores Threshold: 0.83±0.05 | Boxes Display Threshold:0.75<br>Image Sensitivity：0.02-0.04<br>Failure Scores Threshold: >0.94 |

<details>
<summary style="font-weight: bold;">Camera Setup</summary>
  
  ### **Camera Setup**

  | **Endoscope** | **WebCam** |
  |:--|:--|
  | - NozzleCam kits: [StealthBurner](https://www.sliceengineering.com/products/stealthburner-nozzle-camera-kit), [3Do](https://kb-3d.com/store/electronics/779-3do-nozzle-camera-kit.html), etc.<br>- [Build](https://www.instructables.com/3D-Printer-Layer-Cam-Nozzle-Cam-Prusa-Mini/) yours from [Aliexpress](https://s.click.aliexpress.com/e/_AZAMf2) or [Amazon](https://www.amazon.com/dp/B09NVYXTG5?psc=1&ref=ppx_yo2ov_dt_b_product_details) or [Temu](https://www.temu.com/search_result.html?search_key=endoscope%20camera).<br>- Built-in LED **backlighting**.<br>- Positioned **5-10 cm** from the nozzle.<br>- Fixed Focus Lens | Logitech C920, C270<br>AutoFocus enabled<br>Desk Lamp to provide enough light <br>Positioned front left/right corner of printing bed |

  Ensure your camera:
  - [30Hz frame rate, 16:9, >=480P❓](https://community.octoprint.org/t/how-can-i-change-mjpg-streamer-parameters-on-octopi/203)

  ⚠️ Cleaning the camera lens before EACH print is highly recommended for dust removal.


  ### Fixture
  Search and print a camera fixture for your camera model from Thingiverse or Printables. 
</details>

### Cellphone

To enable notifications, enter your [Telegram bot token and chat ID](https://gist.github.com/nafiesl/4ad622f344cd1dc3bb1ecbe468ff9f8a)
or [Discord Webhook url](https://progr.interplanety.org/en/how-to-get-the-discord-channel-webhook-url/). Upon configuration and clicking "Save". A welcome message confirms successful setup. An example failure notification will be sent like this:

<img src="/assets/images/telegram_notification.jpg" width=50% height=50%>

### **Software Configuration**

Navigate to the PiNozCam tab:

<img src="/assets/images/tab.png" width=50% height=50%>

The screenshot:

<img src="/assets/images/screenshot.png" width=50% height=50%>


**Key Parameters:**

- **Action:** Specifies the action PiNozCam should take when a print failure is detected (e.g., notify only, pause print, stop print). Detected failures are displayed in the video stream for 5 seconds, allowing for immediate visual verification.
- **Image Sensitivity:** Adjust the sensitivity to ensure accurate detection of print failures. Set the threshold to balance between premature stopping for minor issues and delaying action for significant errors. A starting value of 0.04 or 4% is recommended for optimal balance.
- **Failure Scores Threshold:** Define the confidence level at which an anomaly is considered a print failure. This setting helps in reducing false alarms by setting a minimum probability threshold for errors, ensuring that only genuine failures prompt action.
- **Max Failure Count:** Specify the number of detections required in **Failure Consider Time** before PiNozCam takes the configured action. A value above 1 is recommended to avoid false positives.
- **Failure Consider Time (s):** Implement a time buffer to focus on recent failures, ignoring older detections that may no longer be relevant. This dynamic consideration helps in adapting to the current state of the print.
- **CPU Speed Control:** Offers options for running the CPU at half or full speed. Half speed is recommended.

Initially, stick with the default settings and adjust them gradually to fine-tune performance.


## Support

For further discussion and support, please [**join our Discord channel**](https://discord.gg/gv4tKJ2ZKr).

## Support my work

I created this plugin in my spare time, so if you have enjoyed using it then please [support it’s development!](https://github.com/sponsors/DrAlexLiu)