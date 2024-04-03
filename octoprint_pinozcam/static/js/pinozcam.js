$(function () {
    function PiNozCAMViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        self.currentEnableAI = ko.observable();
        self.newEnableAI = ko.observable();

        self.currentAction = ko.observable();
        self.newAction = ko.observable("");

        self.currentPrintLayoutThreshold = ko.observable();
        self.newPrintLayoutThreshold = ko.observable();
        self.newPrintLayoutThreshold.subscribe(function(newPrintLayoutThreshold) {
            var newFloatPrintLayoutThreshold = parseFloat(newPrintLayoutThreshold); 
            if (isNaN(newFloatPrintLayoutThreshold) || newFloatPrintLayoutThreshold < 0 || newFloatPrintLayoutThreshold > 1.0) {
                alert("Boxes Display Threshold must be between 0 and 1.");
                self.newPrintLayoutThreshold(undefined); 
            }
        });


        self.currentImgSensitivity = ko.observable();
        self.newImgSensitivity = ko.observable();
        self.newImgSensitivity.subscribe(function(newImgSensitivity) {
            var newFloatImgSensitivity = parseFloat(newImgSensitivity); 
            if (isNaN(newFloatImgSensitivity) || newFloatImgSensitivity < 0 || newFloatImgSensitivity > 1.0) {
                alert("Image Sensitivity must be between 0 and 1.");
                self.newImgSensitivity(undefined); 
            }
        });

        self.currentScoresThreshold = ko.observable();
        self.newScoresThreshold = ko.observable();
        self.newScoresThreshold.subscribe(function(newScoresThreshold) {
            var newFloatScoresThreshold = parseFloat(newScoresThreshold); 
            if (isNaN(newFloatScoresThreshold) || newFloatScoresThreshold < 0 || newFloatScoresThreshold > 1.0) {
                alert("Failure Scores Threshold must be between 0 and 1.");
                self.newScoresThreshold(undefined); 
            }
        });

        self.currentMaxCount = ko.observable();
        self.newMaxCount = ko.observable();
        self.newMaxCount.subscribe(function(newMaxCount) {
            var newIntMaxCount = parseInt(newMaxCount, 10); 
            if (isNaN(newIntMaxCount) || newIntMaxCount < 1 || newIntMaxCount > 100) {
                alert("Max Failure Count must be between 1 and 100.");
                self.newMaxCount(undefined); 
            }
        });

        self.currentCountTime = ko.observable();
        self.newCountTime = ko.observable();
        self.newCountTime.subscribe(function(newCountTime) {
            var newIntCountTime = parseInt(newCountTime, 10); 
            if (isNaN(newIntCountTime) || newIntCountTime < 1 || newIntCountTime > 60000) {
                alert("Failure Consider Time must be between 1 and 60000 seconds.");
                self.newCountTime(undefined); 
            }
        });

        self.currentCpuSpeedControl = ko.observable();
        self.newCpuSpeedControl = ko.observable("");

        self.currentCustomSnapshotURL = ko.observable();
        self.newCustomSnapshotURL = ko.observable();



        self.currentMaxNotification = ko.observable();
        self.newMaxNotification = ko.observable();
        self.newMaxNotification.subscribe(function(newMaxNotification) {
            var newIntMaxNotification = parseInt(newMaxNotification, 10); 
            if (isNaN(newIntMaxNotification) || newIntMaxNotification < 0 || newIntMaxNotification > 60000) {
                alert("Max Notification Count must be between 0 and 60000.");
                self.newMaxNotification(undefined); 
            }
        });

        self.currentTelegramBotToken = ko.observable();
        self.newTelegramBotToken = ko.observable();

        self.currentTelegramChatId = ko.observable();
        self.newTelegramChatId = ko.observable();

        self.currentDiscordWebhookURL = ko.observable();
        self.newDiscordWebhookURL = ko.observable();

        self.onBeforeBinding = function () {
            var pluginSettings =
                self.settingsViewModel.settings.plugins.pinozcam;
            
            self.newEnableAI(pluginSettings.enableAI().toString());
            self.currentEnableAI(self.newEnableAI());    
            
            self.newAction(pluginSettings.action().toString());  
            self.currentAction(self.newAction());

            self.newPrintLayoutThreshold(pluginSettings.printLayoutThreshold());
            self.currentPrintLayoutThreshold(self.newPrintLayoutThreshold());

            self.newImgSensitivity(pluginSettings.imgSensitivity());
            self.currentImgSensitivity(self.newImgSensitivity());

            self.newScoresThreshold(pluginSettings.scoresThreshold());
            self.currentScoresThreshold(self.newScoresThreshold());

            self.newMaxCount(pluginSettings.maxCount());
            self.currentMaxCount(self.newMaxCount());

            self.newCountTime(pluginSettings.countTime());
            self.currentCountTime(self.newCountTime());

            self.newCpuSpeedControl(pluginSettings.cpuSpeedControl().toString());
            self.currentCpuSpeedControl(self.newCpuSpeedControl());

            self.newCustomSnapshotURL(pluginSettings.customSnapshotURL());
            self.currentCustomSnapshotURL(self.newCustomSnapshotURL());

            self.newMaxNotification(pluginSettings.maxNotification());
            self.currentMaxNotification(self.newMaxNotification());

            self.newTelegramBotToken(pluginSettings.telegramBotToken());
            self.currentTelegramBotToken(self.newTelegramBotToken());

            self.newTelegramChatId(pluginSettings.telegramChatID());
            self.currentTelegramChatId(self.newTelegramChatId());
            
            self.newDiscordWebhookURL(pluginSettings.discordWebhookURL());
            self.currentDiscordWebhookURL(self.newDiscordWebhookURL());
        };

        self.saveSettings = function () {
            var newSettings = {
                enableAI: self.newEnableAI() === "true",
                action: parseInt(self.newAction(), 0),
                printLayoutThreshold: parseFloat(self.newPrintLayoutThreshold()),
                imgSensitivity: parseFloat(self.newImgSensitivity()),
                scoresThreshold: parseFloat(self.newScoresThreshold()),
                maxCount: parseInt(self.newMaxCount(), 10), 
                countTime: parseInt(self.newCountTime(), 10), 
                cpuSpeedControl: parseFloat(self.newCpuSpeedControl()),
                customSnapshotURL: self.newCustomSnapshotURL(),
                maxNotification: parseInt(self.newMaxNotification(), 10),
                telegramBotToken: self.newTelegramBotToken(),
                telegramChatID: self.newTelegramChatId(),
                discordWebhookURL: self.newDiscordWebhookURL(),
            };
            OctoPrint.settings
                .savePluginSettings("pinozcam", newSettings)
                .done(function () {
                    new PNotify({
                        title: "Success",
                        text: "Settings have been saved.",
                        type: "success",
                    });
                    self.currentEnableAI(self.newEnableAI());
                    self.currentAction(self.newAction());
                    self.currentPrintLayoutThreshold(self.newPrintLayoutThreshold());
                    self.currentImgSensitivity(self.newImgSensitivity());
                    self.currentScoresThreshold(self.newScoresThreshold());
                    self.currentMaxCount(self.newMaxCount());
                    self.currentCountTime(self.newCountTime());
                    self.currentCpuSpeedControl(self.newCpuSpeedControl());
                    self.currentCustomSnapshotURL(self.newCustomSnapshotURL());
                    self.currentMaxNotification(self.newMaxNotification());
                    self.currentTelegramBotToken(self.newTelegramBotToken());
                    self.currentTelegramChatId(self.newTelegramChatId());
                    self.currentDiscordWebhookURL(self.newDiscordWebhookURL())
                })
                .fail(function () {
                    new PNotify({
                        title: "Error",
                        text: "Failed to save settings.",
                        type: "error",
                    });
                });
        };
    }

    // Register the plugin's ViewModel
    OCTOPRINT_VIEWMODELS.push([
        PiNozCAMViewModel,
        ["settingsViewModel"], // List of dependencies to inject into the plugin
        ["#tab_plugin_pinozcam"], // List of selectors for all elements the ViewModel should be bound to
    ]);
});

setInterval(function () {
    $.ajax({
        url: "/plugin/pinozcam/check",
        type: "GET",
        dataType: "json",
        success: function (response) {
            console.log("Fetched data:", response);
            //var data = JSON.parse(response);  // Parse the JSON response
            $("#ai-image").attr("src", response.image);  // Update the image source
            $("#failure-count").text("Failure Count: " + response.failureCount);  // Update the failure count display
            $("#ai-status").text("AI Status: " + response.aiStatus);  // Update the AI status display
            $("#cpu-temperature").text("CPU Temperature: " + response.cpuTemperature + "°C");  // Update the CPU temperature display
        },
        error: function (error) {
            console.log("Error fetching data:", error);
        },
    });
}, 500); // Request every 0.5 seconds
