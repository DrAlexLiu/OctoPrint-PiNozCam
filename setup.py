# coding=utf-8
import struct
import sys
import requests
import os

########################################################################################################################
### Do not forget to adjust the following variables to your own plugin.

# The plugin's identifier, has to be unique
plugin_identifier = "pinozcam"

# The plugin's python package, should be "octoprint_<plugin identifier>", has to be unique
plugin_package = "octoprint_pinozcam"

# The plugin's human readable name. Can be overwritten within OctoPrint's internal data via __plugin_name__ in the
# plugin module
plugin_name = "OctoPrint-PiNozCam"

# The plugin's version. Can be overwritten within OctoPrint's internal data via __plugin_version__ in the plugin module
plugin_version = "1.0.2"

# The plugin's description. Can be overwritten within OctoPrint's internal data via __plugin_description__ in the plugin
# module
plugin_description = """An AI-driven Failure Detection"""

# The plugin's author. Can be overwritten within OctoPrint's internal data via __plugin_author__ in the plugin module
plugin_author = "DrAlexLiu"

# The plugin's author's mail address.
plugin_author_email = "liu1111w@uwindsor.ca"

# The plugin's homepage URL. Can be overwritten within OctoPrint's internal data via __plugin_url__ in the plugin module
plugin_url = "https://github.com/DrAlexLiu/OctoPrint-PiNozCAM"

# The plugin's license. Can be overwritten within OctoPrint's internal data via __plugin_license__ in the plugin module
plugin_license = "AGPLv3"

# Any additional requirements besides OctoPrint should be listed here
if struct.calcsize("P") * 8 == 32:
    plugin_requires = [
        "numpy>=1.21.4,<1.26",
        "pillow",
        "pyTelegramBotAPI"
    ]

    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    os_release = {}

    try:
        with open("/etc/os-release") as f:
            for line in f:
                key, value = line.strip().split("=")
                os_release[key] = value.strip('"')
    except (FileNotFoundError, PermissionError):
        raise ValueError("Unable to read /etc/os-release")

    codename_map = {
        "10": "buster",
        "11": "bullseye",
        "12": "bookworm"
    }

    version = os_release.get("VERSION_ID")

    if version in codename_map:
        debian_codename = codename_map[version]
        first_version = f"cp{python_version.replace('.', '')}"
        second_version = first_version
        if sys.version_info.minor <= 7:
            second_version += "m"
        onnxruntime_wheel = f"https://github.com/DrAlexLiu/built-onnxruntime-for-raspberrypi-linux/raw/main/wheels/{debian_codename}/onnxruntime-1.17.1-{first_version}-{second_version}-linux_armv7l.whl"
        
        try:
            response = requests.head(onnxruntime_wheel)
            response.raise_for_status()
            plugin_requires.append(f"onnxruntime @ {onnxruntime_wheel}")
        except requests.exceptions.RequestException as e:
            raise ValueError(f"Failed to access onnxruntime wheel URL: {str(e)}")
    else:
        raise ValueError(f"Unsupported Raspbian version: {version}")
elif struct.calcsize("P") * 8 == 64:
    plugin_requires = [
        "numpy>=1.21.4",
        "pillow",
        "onnxruntime>=1.14.1",
        "pyTelegramBotAPI"
    ]
else:
    raise ValueError("Unsupported architecture")

### --------------------------------------------------------------------------------------------------------------------
### More advanced options that you usually shouldn't have to touch follow after this point
### --------------------------------------------------------------------------------------------------------------------

# Additional package data to install for this plugin. The subfolders "templates", "static" and "translations" will
# already be installed automatically if they exist. Note that if you add something here you'll also need to update
# MANIFEST.in to match to ensure that python setup.py sdist produces a source distribution that contains all your
# files. This is sadly due to how python's setup.py works, see also http://stackoverflow.com/a/14159430/2028598
plugin_additional_data = []

# Any additional python packages you need to install with your plugin that are not contained in <plugin_package>.*
plugin_additional_packages = []

# Any python packages within <plugin_package>.* you do NOT want to install with your plugin
plugin_ignored_packages = []

# Additional parameters for the call to setuptools.setup. If your plugin wants to register additional entry points,
# define dependency links or other things like that, this is the place to go. Will be merged recursively with the
# default setup parameters as provided by octoprint_setuptools.create_plugin_setup_parameters using
# octoprint.util.dict_merge.
#
# Example:
#     plugin_requires = ["someDependency==dev"]
#     additional_setup_parameters = {"dependency_links": ["https://github.com/someUser/someRepo/archive/master.zip#egg=someDependency-dev"]}
# "python_requires": ">=3,<4" blocks installation on Python 2 systems, to prevent confused users and provide a helpful error. 
# Remove it if you would like to support Python 2 as well as 3 (not recommended).
additional_setup_parameters = {
    "python_requires": ">=3,<4",
    "dependency_links": []
}

########################################################################################################################

from setuptools import setup

try:
    import octoprint_setuptools
except:
    print(
        "Could not import OctoPrint's setuptools, are you sure you are running that under "
        "the same python installation that OctoPrint is installed under?"
    )
    import sys

    sys.exit(-1)

setup_parameters = octoprint_setuptools.create_plugin_setup_parameters(
    identifier=plugin_identifier,
    package=plugin_package,
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    mail=plugin_author_email,
    url=plugin_url,
    license=plugin_license,
    requires=plugin_requires,
    additional_packages=plugin_additional_packages,
    ignored_packages=plugin_ignored_packages,
    additional_data=plugin_additional_data,
)

if len(additional_setup_parameters):
    from octoprint.util import dict_merge

    setup_parameters = dict_merge(setup_parameters, additional_setup_parameters)

setup(**setup_parameters)
