from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from cyberfeed import CyberNewsFeed
import os
from dotenv import load_dotenv
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()
port = int(os.getenv("PORT"))

limiter = Limiter(key_func=get_remote_address)

# init fast api
app = FastAPI()
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# enable cors for React Native
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# init the CyberNewsFeed
feed = CyberNewsFeed()


@app.get("/news")
@limiter.limit("5/minute")
def get_news():
    # Get latest cyber news articles
    articles=[]
    for source in feed.config['sources']:
        fetched_articles = feed.get_articles(source)
        print(f"Fetched {len(fetched_articles)} articles from {source['name']}")
        articles.extend(fetched_articles)

    return {"articles":[article.__dict__ for article in articles]}

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=port)

