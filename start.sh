echo "Starting Spotify Bot..."

if ! command -v java &> /dev/null; then
    echo "Error: Java is not installed!"
    exit 1
fi

echo "Java version:"
java -version

if ! command -v python3 &> /dev/null; then
    echo "Error: Python3 is not installed!"
    exit 1
fi

if [ ! -f "lavalink/Lavalink.jar" ]; then
    echo "Error: Lavalink.jar not found in lavalink folder!"
    echo "Please download Lavalink.jar from:"
    echo "https://github.com/lavalink-devs/Lavalink/releases/download/4.0.0/Lavalink.jar"
    echo "and place it in the lavalink folder."
    exit 1
fi

if [ ! -f "lavalink/application.yml" ]; then
    echo "Error: application.yml not found in lavalink folder!"
    exit 1
fi

if [ ! -f "requirements.txt" ]; then
    echo "Error: requirements.txt not found!"
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "Error: .env file not found!"
    exit 1
fi

if grep -q "your_bot_token_here" .env; then
    echo "Please edit .env file and add your bot token!"
    exit 1
fi

echo "Installing Python dependencies..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt

echo "Starting Lavalink server..."
cd lavalink
java -jar Lavalink.jar &
LAVALINK_PID=$!
cd ..

echo "Waiting for Lavalink to start..."
sleep 5

while ! curl -s http://localhost:7183/v3/version > /dev/null; do
    echo "Waiting for Lavalink to be ready..."
    sleep 5
done

echo "Lavalink started successfully!"

echo "Starting Discord bot..."
python3 bot.py

wait $LAVALINK_PID 