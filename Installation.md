# A Simplified Guide to Installing a Nozzle Camera on Your 3D Printer

Unveil the hidden aspects of your 3D printing process by installing an endoscope camera on your printer. This setup has been tested on various printers including the Prusa Mini, though it's adaptable to other models like Creality and more. It's assumed your printer is already integrated with OctoPrint on a Raspberry Pi.

## Required Items:

- A 5.5mm Endoscope Camera (search for "Endoscope Camera" on Amazon or AliExpress. Our tests were conducted on the TAKMLY Endoscope Camera.)
- A compatible 3D-printed mount for your 3D printer. (For Prusa Mini, a tested mount can be found [here](https://www.printables.com/model/432351-prusa-mini-endoscope-nozzle-cam-mount). Ensure to find a nozzle camera mount suitable for your printer model.)
- M3 screws and nuts for mounting, as dictated by the mount bracket specifications.
- A wrench for installing screws and nuts for the bracket.

## Preparing the Mount:

1. Obtain and print the endoscope mount suitable for your 3D printer. It's recommended to use PETG for the mount to ensure it can withstand the higher temperatures generated during camera operation.

## Camera Preparation:

1. **Link the camera to OctoPrint:**
   a. Utilize the USB-C to USB adapter (included with the camera) to connect the camera to your Raspberry Pi running OctoPrint.
   b. Access OctoPrint to confirm the camera's functionality. A restart of OctoPrint might be required if the image isn't displayed.

## Camera Orientation:

1. Given the cylindrical form of the camera, orientation can be a tad challenging without a live feed. Determine the camera's direction by examining the live feed, marking the 'up' position. Minor adjustments can be made later for a perfect alignment.

## Camera Mounting:

1. Insert the endoscope into the mount ensuring that the camera nozzle aligns well with OctoPrint’s webcam stream.
2. The optimal camera position is 30-50mm from the nozzle, slightly angled downwards, although the camera has a focal length of 30-100mm.

## Cable Organization:

1. Before finalizing the camera position with the set screw, use zip ties to manage the cables along the existing cable loom. This step ensures the cables are securely fixed, preventing any movement during printing operations. Test the setup by manually moving the toolhead.

## Securing the Camera:

1. Once you're satisfied with the camera's position, tighten the set screw to secure it. If the fit isn't snug, consider wrapping some masking or electrical tape around the camera base for a tighter fit.

## Enhancing Camera Resolution:

> **Tip:** The steps below are for replacing an existing camera. For setting up a secondary camera, refer to external guides, ensuring you set the camera resolution to 640x480.

1. The default camera stream is set at a resolution of 640x480, which is adequate for AI detection purposes. Lower resolutions are not recommended as they may compromise the quality necessary for effective detection.
   a. To optimize the frame rate for better image quality, SSH into your Raspberry Pi running OctoPrint and enter the command: `sudo nano /boot/octopi.txt`
   b. In the nano text editor, navigate and change the line `#camera="auto"` to `camera="usb"` and `#camera_usb_options="-r 640x480 -f 10"` to `camera_usb_options="-r 640x480 -f 10"` or `camera_usb_options="-r 640x480 -f 5"` depending on your preference.
   c. Save and exit the editor by pressing `Control-O` on your keyboard, then `Enter` to confirm the file name.
   d. Restart the webcamd service with the command: `sudo service webcamd restart`

**Note:** Tweaking the frame rate to 10 or 5 frames per second can help in achieving smoother video feeds. This setup, dubbed the PiNozCam, was tested on a Prusa Mini, striving to offer a universal solution for close-up monitoring of 3D printing processes across various printer models.

This guide simplifies the nozzle camera setup process on your 3D printer, enhancing your real-time monitoring capability over your prints.
