
## Architecture Diagram

![Component Diagram](docs/component.png)

**Основные компоненты:**

- **User (Пользователь)** – заполняет форму и нажимает «Generate».
- **Streamlit (Frontend)** – веб-приложение, отправляет запрос к backend и получает файл.
- **FastAPI (Backend / API)** – принимает POST-запросы, валидирует данные, координирует AI Core и Document Engine, возвращает DOCX.
- **AI Core** – формирует промпт, отправляет в OpenAI, получает текст.
- **OpenAI API** – облачный сервис, возвращает сгенерированный текст.
- **Document Engine (python-docx)** – вставляет текст в template.docx, формирует финальный DOCX.
- **template.docx** – DOCX-шаблон с стилями, placeholders и логотипом.



## Sequence Diagram

![Sequence Diagram](docs/sequence.png)

**Step-by-step flow:**

1. **User → Streamlit:** заполняет поля (название компании, контакт, требования, тон) и нажимает «Generate».
2. **Streamlit → FastAPI (POST /generate, JSON body):**  
```json
{
  "client_name": "Example Corp",
  "project_goal": "Automate proposal generation",
  "scope": "...",
  "technologies": ["Python", "FastAPI", "OpenAI"],
  "deadline": "2025-10-31",
  "tone": "Formal"
}
````

3. **FastAPI → AI Core:** формирует промпт и отправляет в OpenAI.
4. **AI Core → OpenAI API:** получает сгенерированный текст.
5. **FastAPI → Document Engine:** парсит текст и формирует DOCX.
6. **Document Engine → FastAPI:** возвращает готовый файл.
7. **FastAPI → Streamlit:** отдаёт DOCX пользователю для скачивания.

```
