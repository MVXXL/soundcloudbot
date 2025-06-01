@echo off
title Spotify Bot
echo Starting Spotify Bot...

java -version >nul 2>&1
if errorlevel 1 (
    echo Java is not installed! Please install Java first.
    pause
    exit /b 1
)

if not exist "lavalink\Lavalink.jar" (
    echo Error: Lavalink.jar not found in lavalink folder!
    echo Please download Lavalink.jar from:
    echo https://github.com/lavalink-devs/Lavalink/releases/download/4.0.0/Lavalink.jar
    echo and place it in the lavalink folder.
    pause
    exit /b 1
)

if not exist "lavalink\application.yml" (
    echo application.yml not found in lavalink folder!
    pause
    exit /b 1
)

echo Starting Lavalink server...
cd lavalink
start /B java -jar Lavalink.jar
cd ..

echo Waiting for Lavalink to start...
timeout /t 5 /nobreak > nul

echo Starting Discord bot...
python bot.py

taskkill /F /IM java.exe >nul 2>&1