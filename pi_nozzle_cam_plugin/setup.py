from setuptools import setup

plugin_identifier = "pi_nozzle_cam_plugin"
plugin_name = "PiNozzleCam"
plugin_version = "0.1.0"
plugin_description = """OctoPrint Plugin for PiNozzleCam"""
plugin_author = "John Doe"
plugin_url = ""
plugin_license = "AGPLv3"

setup(
    name=plugin_name,
    version=plugin_version,
    description=plugin_description,
    author=plugin_author,
    url=plugin_url,
    license=plugin_license,
    packages=["octoprint_" + plugin_identifier],
    include_package_data=True,
    install_requires=[
        "setuptools",
    ],
    entry_points={
        "octoprint.plugin": [
            "pi_nozzle_cam_plugin = octoprint" + plugin_identifier,
        ]
    },
)
