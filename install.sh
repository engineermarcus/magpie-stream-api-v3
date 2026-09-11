# Install Python 3.11 if not already present
sudo apt-get update && sudo apt-get install -y python3.11 python3.11-venv python3-pip

# Also install Playwright's system deps
sudo apt-get install -y \
    wget curl gnupg ca-certificates \
    fonts-liberation libglib2.0-0 libnss3 libatk1.0-0 \
    libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
    libgbm1 libasound2 libpango-1.0-0 libcairo2

# Install deps
pip install -r requirements.txt --break-system-packages 

# Install Chromium for Playwright
playwright install chromium --with-deps


