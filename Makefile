.PHONY: run install logs

run:
	python main.py

install:
	python -m venv venv
	./venv/bin/pip install -r requirements.txt
	sudo cp systemd/voltahub-bridge.service /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable voltahub-bridge

logs:
	sudo journalctl -u voltahub-bridge -f
