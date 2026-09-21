"""Start the Mass Mailer:  python run.py"""
import uvicorn

from backend.app.config import settings

if __name__ == "__main__":
    print(f"\n  Mass Mailer ({settings.email_mode} mode) -> http://{settings.host}:{settings.port}\n")
    uvicorn.run("backend.app.main:app", host=settings.host, port=settings.port, workers=1)
