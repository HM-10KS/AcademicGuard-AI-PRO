from pathlib import Path
import os
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent/'.env')
import uvicorn
uvicorn.run('app:app',host='127.0.0.1',port=8000,reload=True)
