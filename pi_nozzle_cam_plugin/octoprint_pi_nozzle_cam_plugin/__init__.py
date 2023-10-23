import octoprint.plugin


class ExampleTabPlugin(
    octoprint.plugin.StartupPlugin,
    octoprint.plugin.TemplatePlugin,
    octoprint.plugin.AssetPlugin,
):
    # Return the templates you defined
    def get_template_configs(self):
        return [
            {"type": "navbar", "template": "navbar.jinja2", "suffix": "example"},
            {"type": "tab", "template": "example_tab.jinja2", "custom_bindings": True},
        ]

    # If you have any JS or CSS files, you'll include them here
    def get_assets(self):
        return dict(
            js=["js/example.js"], css=["css/example.css"], less=["less/example.less"]
        )


__plugin_implementation__ = ExampleTabPlugin()

# If you want to make sure your plugin is compatible with Python 3, you'd also add the following:
__plugin_pythoncompat__ = ">=2.7,<4"
