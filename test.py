import os
from dotenv import load_dotenv
from google import genai

load_dotenv()  # 載入 .env 檔案裡的環境變數

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

response = client.models.generate_content(
    model="gemini-3-flash-preview", contents="Explain how AI works in a few words"
)
print(response.text)