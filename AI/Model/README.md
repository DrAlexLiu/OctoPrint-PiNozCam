# Model and Weight Setup Guide

## 1. Flash the OctoPi
OctoPi is the Raspberry Pi version of OctoPrint. It provides an easy way to run OctoPrint without the need for extra installations.

### Steps to Flash OctoPi:
1. **Download the OctoPi Image**: You can download the latest OctoPi image from the official [OctoPrint download page](https://octoprint.org/download/).
   
2. **Prepare an SD Card**: Ensure you have an SD card that is at least 4GB in size. Format the SD card to ensure it's clean and ready for the OctoPi image.

3. **Write the OctoPi Image to the SD Card**: Use software like [Balena Etcher](https://www.balena.io/etcher/) to flash the OctoPi image onto the SD card. Simply select the OctoPi image you downloaded and the SD card you prepared, then click "Flash".

4. **Insert the SD Card into Raspberry Pi**: Once the flashing process is complete, safely eject the SD card from your computer and insert it into your Raspberry Pi.

5. **Power Up & Setup**: Connect your Raspberry Pi to power and follow on-screen instructions to complete the OctoPi setup.

For a more detailed guide, you can refer to the [official OctoPi setup guide](https://github.com/guysoft/OctoPi).

## 2. Install the Required Packages
Before installing the packages, you'll need to SSH into your OctoPi.

### SSH into OctoPi:
1. **Enable SSH on OctoPi**: By default, SSH is disabled for security reasons. To enable it, place a file named `ssh` (without any extension) onto the boot partition of the SD card.

2. **Connect to OctoPi via SSH**: Use a terminal or an SSH client like [PuTTY](https://www.putty.org/) for Windows. The default credentials are:
   - **Username**: pi
   - **Password**: raspberry

   Use the following command to SSH:
   ```bash
   ssh pi@octopi.local
   ```

To install the necessary packages, navigate to the directory containing the `.whl` files and run the following command:
```bash
pip install packages/*.whl
```
## 3. Run the Monitoring Script
Execute the monitor.py script using the following command:

```bash
python monitor.py
```
## 4. Testing Without Local Streaming

If you want to test the setup without using local streaming, you can change the streaming mode to use local pictures instead.

Open the script monitor.py where the `use_streaming` variable is set and modify it:

```python
use_streaming=False
```
