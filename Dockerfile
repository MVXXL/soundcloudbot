FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y openjdk-17-jre-headless && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p lavalink

RUN wget https://github.com/lavalink-devs/Lavalink/releases/download/4.0.8/Lavalink.jar -O lavalink/Lavalink.jar

RUN echo '#!/bin/bash\n\
echo "Starting Spotify Bot..."\n\
\n\
if ! command -v java &> /dev/null; then\n\
    echo "Java is not installed!"\n\
    exit 1\n\
fi\n\
\n\
if [ ! -f "lavalink/Lavalink.jar" ]; then\n\
    echo "Lavalink.jar not found!"\n\
    exit 1\n\
fi\n\
\n\
if [ ! -f "lavalink/application.yml" ]; then\n\
    echo "application.yml not found!"\n\
    exit 1\n\
fi\n\
\n\
java -Xmx2G -Xms1G -XX:+UseG1GC -XX:G1ReservePercent=20 -XX:InitiatingHeapOccupancyPercent=35 -jar lavalink/Lavalink.jar &\n\
\n\
sleep 10\n\
\n\
python bot.py\n\
\n\
pkill -f "java.*Lavalink.jar"' > start.sh && \
chmod +x start.sh

ENV PYTHONUNBUFFERED=1

CMD ["./start.sh"] 