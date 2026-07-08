# VinBDI Realtime Client for Motion

## Usage
```sh
# install
apt install portaudio19-dev
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# combine demo
# press q to exit, say HeyMotion or press 'w' to wake
# voice-llm will exit after 20s timeout
python main.py 

# project terminal + fixed ports
./run_project_server.sh

# small demo
python cli_voice.py  # press Enter to exit
```

## Ref
- https://vinmotion.atlassian.net/wiki/spaces/VMVB/pages/15597583/250325+VBD+Realtime+Client
- https://vinmotion.atlassian.net/wiki/spaces/VMVB/pages/24969222/250408+VBD+Wuw+Client
