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
To install the necessary packages, navigate to the directory containing the `.whl` files and run the following command:
```bash
pip install packages/*.whl
```
## 3. Run the Monitoring Script
Execute the monitor.py script using the following command:

```bash
python monitor.py
```

