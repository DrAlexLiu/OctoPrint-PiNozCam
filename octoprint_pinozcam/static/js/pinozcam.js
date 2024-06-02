$(function () {
    function PiNozCAMViewModel(parameters) {
        var self = this;

        self.settingsViewModel = parameters[0];

        self.currentMaskImageData = ko.observable('0'.repeat(4096));
        self.newMaskImageData = ko.observable('0'.repeat(4096));

        self.currentEnableAI = ko.observable();
        self.newEnableAI = ko.observable();

        self.currentAction = ko.observable();
        self.newAction = ko.observable("");

        self.currentAiStartDelay = ko.observable();
        self.newAiStartDelay = ko.observable();
        self.newAiStartDelay.subscribe(function(newAiStartDelay) {
            var parsedAiStartDelay = parseInt(newAiStartDelay, 10); 
            if (isNaN(parsedAiStartDelay) || parsedAiStartDelay < 0 || parsedAiStartDelay > 60000) {
                alert("AI Start Delay must be between 0 and 60000 seconds.");
                self.newAiStartDelay(undefined); 
            }
        });

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

        self.currentEnableMaxFailureCountNotification = ko.observable();
        self.newEnableMaxFailureCountNotification = ko.observable();

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

        self.handleMaskDialog = function() {
            var maskDialog = document.getElementById('mask-dialog');
            var openDialogBtn = document.getElementById('open-dialog-btn');
            var saveMaskBtn = document.getElementById('save-mask-btn');
            var clearMaskBtn = document.getElementById('clear-mask-btn');
            var cancelMaskBtn = document.getElementById('cancel-mask-btn');

            var maskCanvas = document.getElementById('mask-canvas');
            var maskContext = maskCanvas.getContext('2d');

            var isDrawing = false;
            var maskWidth = 20;
            //mask matrix
            var tempMaskImageData = Array.from({ length: 64 }, () => Array(64).fill(0));

            let tempClearMaskImageData = '0'.repeat(4096); 

            function loadBackgroundImage() {
                var aiImage = document.getElementById('ai-image');
                var aiImageUrl = aiImage.src;
        
                var backgroundImage = new Image();
                backgroundImage.src = aiImageUrl;
                backgroundImage.onload = function() {
                    maskCanvas.width = backgroundImage.width;
                    maskCanvas.height = backgroundImage.height;
                    maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
                    maskContext.drawImage(backgroundImage, 0, 0, maskCanvas.width, maskCanvas.height);
                    var blockWidth = Math.ceil(maskCanvas.width / 64);
                    var blockHeight = Math.ceil(maskCanvas.height / 64);
        
                    if (self.currentMaskImageData()) {
                        var maskMatrix = decompressMaskMatrix(self.currentMaskImageData());
    
                        for (var i = 0; i < maskMatrix.length; i++) {
                            for (var j = 0; j < maskMatrix[i].length; j++) {
                                if (maskMatrix[i][j]) {
                                    
                                    maskContext.fillRect(j * blockWidth, i * blockHeight, blockWidth, blockHeight);
                                    maskContext.strokeStyle = 'red';
                                    maskContext.lineWidth = 1;
                                    maskContext.strokeRect(j * blockWidth, i * blockHeight, blockWidth, blockHeight);
                                }
                            }
                        }
                    }

                };
            }

            function startDrawing(e) {
                isDrawing = true;
                drawMask(e);
            }

            function drawMask(e) {
                if (!isDrawing) return;
        
                var rect = maskCanvas.getBoundingClientRect();
                var scaleX = maskCanvas.width / rect.width;
                var scaleY = maskCanvas.height / rect.height;
                var x = (e.clientX - rect.left) * scaleX;
                var y = (e.clientY - rect.top) * scaleY;
        
                var row = Math.floor(y * 64 / maskCanvas.height);
                var col = Math.floor(x * 64 / maskCanvas.width);
                tempMaskImageData[row][col] = 1;
        
                maskContext.beginPath();
                maskContext.arc(x, y, maskWidth / 2, 0, Math.PI * 2);
                maskContext.fillStyle = 'black';
                maskContext.fill();
                maskContext.closePath();
            }

            function stopDrawing() {
                isDrawing = false;
            }

            function compressMaskMatrix(maskMatrix) {
                return maskMatrix.flat().map(cell => cell ? '1' : '0').join('');
            }
            
            function decompressMaskMatrix(compressedMaskMatrix) {
                return Array.from({ length: 64 }, (_, i) =>
                    Array.from({ length: 64 }, (_, j) => compressedMaskMatrix[i * 64 + j] === '1')
                );
            }

            maskCanvas.addEventListener('mousedown', startDrawing);
            maskCanvas.addEventListener('mousemove', drawMask);
            maskCanvas.addEventListener('mouseup', stopDrawing);
            maskCanvas.addEventListener('mouseleave', stopDrawing);

            openDialogBtn.addEventListener('click', function() {
                loadBackgroundImage();
                maskDialog.showModal();
            });

            saveMaskBtn.addEventListener('click', function() {
                // Compress the current mask data into a string
                var compressedMaskMatrix = tempMaskImageData.flat().join('');

                // Get the previous mask data
                var previousMaskImageData = self.currentMaskImageData();

                // Decompress the previous mask data into a 2D boolean array
                var previousMaskMatrix = decompressMaskMatrix(previousMaskImageData);

                // Decompress the new mask data into a 2D boolean array
                var newMaskMatrix = decompressMaskMatrix(compressedMaskMatrix);

                // Merge the previous mask data with the new mask data
                var mergedMaskMatrix = previousMaskMatrix.map((row, i) => row.map((cell, j) => cell || newMaskMatrix[i][j]));

                // Compress the merged mask data into a string
                var mergedCompressedMaskMatrix = compressMaskMatrix(mergedMaskMatrix);

                // Update the mask data and save the settings
                self.newMaskImageData(mergedCompressedMaskMatrix);
                self.saveSettings();
                tempClearMaskImageData = '0'.repeat(4096);
                maskDialog.close();
            });

            clearMaskBtn.addEventListener('click', function() {
                maskContext.clearRect(0, 0, maskCanvas.width, maskCanvas.height);
                tempMaskImageData = Array.from({ length: 64 }, () => Array(64).fill(0));
                //self.newMaskImageData('0'.repeat(4096));
                tempClearMaskImageData = self.currentMaskImageData();
                self.currentMaskImageData('0'.repeat(4096));
                //self.saveSettings();
                setTimeout(function() {
                    loadBackgroundImage();
                }, 500); 
            });
        
            cancelMaskBtn.addEventListener('click', function() {
                if (tempClearMaskImageData !== '0'.repeat(4096)) {
                    self.currentMaskImageData(tempClearMaskImageData);
                }
                maskDialog.close();
            });
        };

        self.onBeforeBinding = function () {
            var pluginSettings =
                self.settingsViewModel.settings.plugins.pinozcam;

            self.newMaskImageData(pluginSettings.maskImageData());
            self.currentMaskImageData(self.newMaskImageData());
            
            self.newEnableAI(pluginSettings.enableAI().toString());
            self.currentEnableAI(self.newEnableAI());    
            
            self.newAction(pluginSettings.action().toString());  
            self.currentAction(self.newAction());

            self.newAiStartDelay(pluginSettings.aiStartDelay());
            self.currentAiStartDelay(self.newAiStartDelay());

            self.newPrintLayoutThreshold(pluginSettings.printLayoutThreshold());
            self.currentPrintLayoutThreshold(self.newPrintLayoutThreshold());

            self.newImgSensitivity(pluginSettings.imgSensitivity());
            self.currentImgSensitivity(self.newImgSensitivity());

            self.newScoresThreshold(pluginSettings.scoresThreshold());
            self.currentScoresThreshold(self.newScoresThreshold());

            self.newMaxCount(pluginSettings.maxCount());
            self.currentMaxCount(self.newMaxCount());

            self.newEnableMaxFailureCountNotification(pluginSettings.enableMaxFailureCountNotification().toString());
            self.currentEnableMaxFailureCountNotification(self.newEnableMaxFailureCountNotification());

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
                maskImageData: self.newMaskImageData(),
                enableAI: self.newEnableAI() === "true",
                action: parseInt(self.newAction(), 0),
                aiStartDelay: parseInt(self.newAiStartDelay(), 10),
                printLayoutThreshold: parseFloat(self.newPrintLayoutThreshold()),
                imgSensitivity: parseFloat(self.newImgSensitivity()),
                scoresThreshold: parseFloat(self.newScoresThreshold()),
                maxCount: parseInt(self.newMaxCount(), 10), 
                enableMaxFailureCountNotification: self.newEnableMaxFailureCountNotification() === "true",
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
                    self.currentMaskImageData(self.newMaskImageData());
                    self.currentEnableAI(self.newEnableAI());
                    self.currentAction(self.newAction());
                    self.currentAiStartDelay(self.newAiStartDelay());
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

        self.onStartupComplete = function() {
            self.handleMaskDialog();
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