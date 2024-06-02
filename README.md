# OctoPrint-PiNozCam
<div>
  <img src="/assets/images/failure_detection1.jpg" width="40%" height="40%">
  <img src="/assets/images/failure_detection_side.jpeg" width="48%" height="48%">
</div>

[![Join Discord](https://img.shields.io/discord/1158238902197424251.svg?label=Discord&logo=discord&logoColor=ffffff&color=7389D8&labelColor=555555)](https://discord.gg/gv4tKJ2ZKr)

## Introduction

Welcome to the era of edge computing with free failure detection performed directly on your device

**Device (50% of All Cores for AI)**|**Detect Speed (images / minute)**
:-----:|:-----:
Raspberry Pi 5|45
Raspberry Pi 4|9

<details>
  <summary>More Support Devices</summary>
  
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
| **Auto Pause/Stop** | **No Subscriptions** | **Set Undetect Zone** |

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
  ✅|✅|✅|✅|>=1GB
</details>

<details>
<summary>Plugin Setup</summary>

## Plugin Setup

Install via the bundled [Plugin Manager](https://docs.octoprint.org/en/master/bundledplugins/pluginmanager.html)
or manually using this URL:

    https://github.com/DrAlexLiu/OctoPrint-PiNozCam/archive/master.zip
    
</details>

## One AI, Two Modes

| **Choose Mode and Set Correct Parameters in PiNozCam:** | |
|:--|:--|
| **NozzleCam** | **WebCam** |
| <img src="/assets/images/nozzle_cam_setup.jpg" width=60% height=60%> | <img src="/assets/images/side_camera_setup.jpg" width=40% height=40%> |
| Boxes Display Threshold: 0.6<br>Image Sensitivity：0.05 (0.04-0.1)<br>Failure Scores Threshold: 0.75 (0.75-0.88) | Boxes Display Threshold: 0.75<br>Image Sensitivity：0.02 (0.02-0.04)<br>Failure Scores Threshold: 0.94 (0.94-0.99) |

<details>
<summary style="font-weight: bold;">Camera Setup</summary>
  
  ### **📷Camera Setup**

  | **Endoscope** | **WebCam** |
  |:--|:--|
  | - NozzleCam kits: [StealthBurner](https://www.sliceengineering.com/products/stealthburner-nozzle-camera-kit), [3Do](https://kb-3d.com/store/electronics/779-3do-nozzle-camera-kit.html), etc.<br>- [Build](https://www.instructables.com/3D-Printer-Layer-Cam-Nozzle-Cam-Prusa-Mini/) yours from [Aliexpress](https://s.click.aliexpress.com/e/_AZAMf2) or [Amazon](https://www.amazon.com/dp/B09NVYXTG5?psc=1&ref=ppx_yo2ov_dt_b_product_details) or [Temu](https://www.temu.com/search_result.html?search_key=endoscope%20camera).<br>- Built-in LED **backlighting**.<br>- Positioned **5-10 cm** from the nozzle.<br>- Fixed Focus Lens | Logitech C920, C270<br>AutoFocus enabled<br>Desk Lamp to provide enough light <br>Positioned front left/right corner of printing bed |

  Ensure your camera:
  - [30Hz frame rate, 16:9, >=480P❓](https://community.octoprint.org/t/how-can-i-change-mjpg-streamer-parameters-on-octopi/203)

  ⚠️ Cleaning the camera lens before EACH print is highly recommended for dust removal.

  ### Fixture
  Search and print a camera fixture for your camera model from Thingiverse or Printables. 
</details>

### 📱Telegram/Discord Cellphone Notification

To enable notifications, enter your [Telegram bot token and chat ID](https://gist.github.com/nafiesl/4ad622f344cd1dc3bb1ecbe468ff9f8a)
or [Discord Webhook url](https://progr.interplanety.org/en/how-to-get-the-discord-channel-webhook-url/). Also, [setup your printer title](https://community.octoprint.org/t/how-do-i-change-the-web-interfaces-name/21662), a message carrys the printer title to help you identify your printer. Leave blank to disable notification.

<details>
<summary>Notification Examples</summary>

Upon configuration and clicking "Save". A welcome message confirms successful setup. An example failure notification will be sent like this:

| **Example:** | |
|:--|:--|
| **Telegram** | **Discord** |
| <img src="/assets/images/telegram_notification.jpg" width=50% height=50%> | <img src="/assets/images/discord_notification.png" width=70% height=70%> |

</details>

### **💥Set Undetect Zone:**
Open the dialog box to make a custom mask. This mask tells the AI which parts of the image to ignore when looking for print failures. Draw on the canvas to select the areas you don't want the AI to check. This lets you focus the AI on the most important parts of your print. The mask you draw will be placed on top of the original image when the AI analyzes it for failures.

<img src="/assets/images/mask_background.png" width=30% height=30%>

### **Parameters Adjustment**

Navigate to the PiNozCam tab:

<img src="/assets/images/tab.png" width=40% height=40%>

The screenshot:

<img src="/assets/images/screenshot.png" width=40% height=40%>

**Key Parameters:**

Initially, stick with the default settings and adjust them gradually to fine-tune performance.

- **Action after Detection:** Specifies the action PiNozCam should take when a print failure is detected (e.g., notify only, pause print, stop print). Detected failures are displayed in this webpage for 5 seconds, allowing for immediate visual verification.
- **Image Sensitivity:** Image sensitivity = (All bounding box areas 'higher than Failure Scores Threshold')/(Whole image area). A smaller number will find small failures or when a failure just starts. A bigger number will only find big, easy-to-see failures or failures that have been going on for a while and have gotten larger.
- **Failure Scores Threshold:** Set the smallest score a box needs to count as a failure and make alerts or actions happen. You can see the score in the corner of each box. A higher number means the AI is more certain about failures but could miss some. A lower number means the AI will spot failures sooner but might also give false alarms.
- **Max Failure Count:** Set the maximum number of failures allowed within the Failure Consider Time before PiNozCam pauses or stops the print as configured in Action after Detection. A value 2 or above is recommended to avoid false alarms.
- **Failure Consider Time (s):** Set the time window in seconds that PiNozCam remembers and counts failures towards the **Max Failure Count**. Older failures outside this window are forgotten, like how an airplane's black box only records the last part of the flight.

<details>
<summary>Other Parameters</summary>

- **Enable PiNozCam:** Turn the AI detection function of PiNozCam on or off.
- **AI Start Delay (s):** Set how many seconds PiNozCam should wait after OctoPrint starts a print before it begins looking for failures. This delay gives time for the bed to level and other starting print steps to finish.
- **Notify Mode:** Choose whether to send a notification for each failure detected or only after reaching the **Max Failure Count**.
- **Custom Snapshot URL:** Provide a custom URL or IP camera URL for PiNozCam to fetch camera images from instead of the default snapshot URL. Examples: http://192.168.0.xxx/webcam/?action=snapshot. (RTSP protocol is not supported)
- **CPU Speed Control:** Offers options for running the CPU at half or full speed. Half speed is recommended.
- **Max Notification Count:** Set the maximum number of messages PiNozCam will send before it stops sending more until the print is finished or stopped. If you set it to 0, there will be no limit and it will keep sending messages.

</details>

## Customer Support

For further discussion and support, please [**join our Discord channel**](https://discord.gg/gv4tKJ2ZKr).

<details>
<summary>Support my work</summary>

I created this plugin in my spare time, so if you have enjoyed using it then please [support it’s development!](https://paypal.me/xingchen613)

</details>