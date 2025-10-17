
## Architecture Diagram

![Component Diagram](docs/component_diagram.png)

**Main Components:**

- **User** – fills out the form in the browser and clicks “Generate”.
- **Streamlit (Frontend)** – simple web app; sends requests to backend and receives the file. It’s the only entry point from the user’s perspective.
- **FastAPI (Backend / API)** – receives POST requests, validates input (Pydantic), coordinates AI Core and Document Engine, returns the final DOCX file.
- **AI Core** – generates prompts based on form data and selected options (tone, audience, structure), sends request to OpenAI, receives structured text (Markdown or similar).
- **OpenAI API** – cloud service that returns generated text.
- **Document Engine (python-docx)** – parses Markdown text, applies styles, inserts into `template.docx`, produces the final DOCX file in memory (BytesIO).
- **template.docx** – DOCX template with pre-defined styles (Heading 1/2), logo in headers, placeholders (e.g., `{{title}}`, `{{sections}}`).


## Sequence Diagram

![Sequence Diagram](docs/sequence_diagram.png)

**Step-by-Step Flow:**

1. **User → Streamlit:** fills in fields (company name, contact, requirements, tone) and clicks “Generate”.
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

3. **FastAPI → AI Core:** constructs the prompt and sends it to OpenAI.
4. **AI Core → OpenAI API:** receives generated text.
5. **FastAPI → Document Engine:** parses text and generates DOCX.
6. **Document Engine → FastAPI:** returns the completed file.
7. **FastAPI → Streamlit:** sends the final DOCX back to the user for download.


