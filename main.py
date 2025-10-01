import os
import json
import time
from typing import Any, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from crewai import Agent, Crew, Task
from crewai import LLM

from crewai_tools import ScrapeWebsiteTool
from pydantic import BaseModel, Field, field_validator

import os
from typing import Any, Dict

# Load environment variables
load_dotenv()

import openlit
openlit.init(otlp_endpoint="http://127.0.0.1:4318")


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_DOC_URL = "https://open-meteo.com/en/docs/geocoding-api#geocoding_search"


class GeoSearchParams(BaseModel):
    name: str = Field(..., description="City or place name to search for")
    count: Optional[int] = Field(2, ge=1, le=100, description="Number of results 1..100")
    language: Optional[str] = Field("en", description="IETF language code, e.g., en, es")
    countryCode: Optional[str] = Field(None, description="ISO-3166-1 alpha-2 country filter")
    format: Optional[str] = Field("json", description="Response format, default json")

    @field_validator("language")
    def normalize_language(cls, v: Optional[str]) -> Optional[str]:
        return v.lower() if isinstance(v, str) else v


class ValidationResult(BaseModel):
    valid: bool = Field(..., description="Indicates whether the provided parameters are valid and can be used with the API. True if all parameters are valid, False otherwise.")
    params: GeoSearchParams = Field(..., description="The validated and potentially corrected parameters. If valid is True, these parameters are ready to be used with the API. If valid is False, these may contain suggested corrections.")
    reason: str = Field("", description="Explanation of why the validation passed or failed. If validation failed, this will contain details about what needs to be corrected.")


class QueryGateResult(BaseModel):
    is_city_query: bool = Field(..., description="True if the user's input is about a city/place or clearly requests city information.")
    is_safe: bool = Field(..., description="True if the input is safe and appropriate to process.")
    safetyReason: str = Field("", description="Internal reason explaining why the query was rejected (not shown to user). Include details like policy or format violations.")
    returnMessageToUser: str = Field("", description="User-facing guidance explaining how to phrase a valid city-focused query. Keep abstract; do not reveal internal safety analysis.")


def create_llm(temperature_preset: str = "medium") -> LLM:
    """
    Create an LLM instance with a specified temperature preset.
    
    Args:
        temperature_preset: One of "low" (0.1), "medium" (0.3), or "high" (0.7)
        
    Returns:
        LLM: Configured LLM instance with specified temperature
    """
    temperature_map = {
        "low": 0.1,      # For precise, deterministic tasks
        "medium": 0.3,   # Balanced for most tasks
        "high": 0.7      # For creative tasks
    }
    
    if temperature_preset not in temperature_map:
        raise ValueError(f"Invalid temperature preset: {temperature_preset}. Must be one of: {list(temperature_map.keys())}")
    
    return LLM(
        model="gemini/gemini-2.0-flash",
        temperature=temperature_map[temperature_preset],
        api_key=os.getenv("GEMINI_API_KEY")
    )


def build_agents() -> Tuple[Agent, Agent, Agent, Agent]:
    """
    Create and configure agents with appropriate temperature settings.
    
    Returns:
        Tuple containing four agents: (gatekeeper, parameter_extractor, validator, summarizer)
    """
    # Create agents with appropriate temperature presets
    gatekeeper = Agent(
        role="City Query Gatekeeper",
        backstory=(
            "You are the first line of defense. You ensure the user's input is a safe, appropriate, and city-related "
            "request suitable for the Open-Meteo Geocoding city information workflow."
        ),
        goal=(
            "Evaluate the user's input. If it is a city-related, safe query, return JSON indicating acceptance. "
            "Otherwise, return JSON explaining the workflow and how to rephrase the query to be city-focused."
        ),
        llm=create_llm("medium"),  # Balanced understanding
        tools=[],
        verbose=False
    )
    api_parameter_extractor = Agent(
        role="City Query Parameter Extractor",
        backstory=(
            "You convert natural language questions about a city into parameters for the Open-Meteo "
            "Geocoding API. You only care about city metadata (coords, elevation, population, etc.)."
        ),
        goal=(
            "Given a user question about a city, return ONLY a JSON object for the geocoding search "
            "with fields name, count, language, countryCode, respecting the API."
        ),
        llm=create_llm("low"),  # Precise parameter extraction
        tools=[],
    verbose=False
)

    docs_tool = ScrapeWebsiteTool(website_url=OPEN_METEO_DOC_URL)

    validator = Agent(
        role="Open-Meteo Geocoding Parameter Validator",
        backstory=(
            "You are an expert on Open-Meteo Geocoding API. You consult the official docs to validate "
            "and correct parameters for the search endpoint."
        ),
        goal=(
            "Validate parameters against the official docs and correct them if needed. Return a strict "
            "ValidationResult JSON with valid, params, reason."
        ),
        llm=create_llm("low"),  # Strict validation
        tools=[docs_tool],
        verbose=False
    )

    summarizer = Agent(
        role="City Information Summarizer",
        backstory=(
            "You summarize geocoding search results into short, readable bullet points focusing on city information."
        ),
        goal=(
            "Given a JSON payload from Open-Meteo geocoding search, output 3-6 concise bullet points "
            "covering matched place name(s), country, latitude/longitude, elevation, timezone, population, and postcodes if present."
        ),
        llm=create_llm("high"),
        tools=[],
    verbose=False
)

    return gatekeeper, api_parameter_extractor, validator, summarizer


def make_gate_task(agent: Agent, user_query: str) -> Task:
    description = (
        "Guardrail the user input for a city information workflow.\n"
        "Determine if the input is:\n"
        "- city-related (asks about a city/place or requests city information), and\n"
        "- safe and appropriate to process.\n"
        "If acceptable, respond ONLY as JSON: {\"is_city_query\": true, \"is_safe\": true, \"safetyReason\": \"\", \"returnMessageToUser\": \"\"}.\n"
        "If not acceptable, respond ONLY as JSON with is_city_query=false or is_safe=false and:\n"
        "- safetyReason: internal explanation (e.g., unsafe instruction, non-city topic, policy violation).\n"
        "- returnMessageToUser: an abstract, user-facing guidance like 'Please ask a city-focused question, e.g., “Tell me about Paris, FR” or “Give me details about Tokyo”.'.\n"
        f"User input: {user_query}"
    )
    return Task(description=description, agent=agent, expected_output="JSON only", output_json=QueryGateResult)


def make_param_extraction_task(agent: Agent, user_query: str) -> Task:
    instructions = (
        "Extract parameters for Open-Meteo Geocoding API city search.\n"
        "Rules:\n"
        "- Respond ONLY with a valid GeoSearchParams JSON.\n"
        "- Fields:\n"
        "  - name (required): The city or place name to search for. If not specified, infer from context.\n"
        "  - count (optional, default=10): Number of results (1-100).\n"
        "  - language (optional, default='en'): IETF language code (e.g., 'en', 'es', 'fr'). \n"
        "    - Detect from the user's query if possible (e.g., 'cities in France' → 'fr').\n"
        "    - Use common language codes like 'en' for English, 'es' for Spanish, etc.\n"
        "  - countryCode (optional): ISO-3166-1 alpha-2 country code (e.g., 'US', 'FR', 'JP').\n"
        f"\nUser question: {user_query}\n"
        "\nReturn a valid JSON object with the extracted parameters. Example:\n"
        '{"name": "Paris", "count": 5, "language": "fr", "countryCode": "FR"}'
    )
    return Task(description=instructions, agent=agent, expected_output="JSON only", output_json=GeoSearchParams)


def make_validation_task(agent: Agent, params_json: str) -> Task:
    docs_hint = (
        f"Open-Meteo Geocoding API docs: {OPEN_METEO_DOC_URL}\n"
        "Endpoint: https://geocoding-api.open-meteo.com/v1/search\n"
        "Important rules:\n"
        "- name is required and must be >= 2 chars to match anything useful.\n"
        "- count is optional, default 10, allowed range 1..100.\n"
        "- language is optional (lower-cased IETF code).\n"
        "- countryCode is optional ISO-3166-1 alpha-2.\n"
        "Your tools include a doc scraper configured to this page; consult it if needed.\n"
        "Respond ONLY as a ValidationResult JSON."
    )
    description = (
        f"Validate these parameters against Open-Meteo Geocoding API and correct if needed.\n{docs_hint}\n"
        f"Parameters to validate: {params_json}"
    )
    return Task(description=description, agent=agent, expected_output="JSON only", output_json=ValidationResult)


def make_summarize_task(agent: Agent, payload_json: str, user_query: str) -> Task:
    description = (
        "Create a comprehensive and engaging summary about the city based on the data provided. "
        "Your summary should be well-structured and include the following sections:\n\n"
        "1. **Introduction**: Start with an engaging opening about the city, including its name and country. "
        f"Mention the user's original question: '{user_query}'.\n\n"
        "2. **Geographical Overview**: Include key geographical details like coordinates, elevation, and timezone. "
        "Explain what makes the location unique geographically.\n\n"
        "3. **Demographics**: If available, discuss population statistics and any notable demographic information.\n\n"
        "4. **Historical Context**: Provide a brief historical background or interesting historical facts about the city.\n\n"
        "5. **Interesting Facts**: Include 2-3 fascinating or unique facts about the city.\n\n"
        "6. **Practical Information**: Mention any relevant postcodes details if available.\n\n"
        "Write in a friendly, informative tone as if you're a knowledgeable local guide. "
        "Make it engaging enough to spark the reader's interest in visiting the city.\n\n"
        f"Geocoding data to use: {payload_json}"
    )

    return Task(
        description=description,
        agent=agent,
        expected_output="A well-structured, engaging summary about the city with the specified sections"
    )


def call_geocoding_api(params: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    try:
        response = requests.get(OPEN_METEO_GEOCODING_URL, params=params, timeout=15)
        status = response.status_code
        data: Dict[str, Any] = {}
        try:
            data = response.json()
        except Exception:
            data = {"error": "Non-JSON response"}
        return status, data
    except requests.RequestException as exc:
        return 0, {"error": str(exc)}


def run_city_info_workflow(user_query: str) -> Optional[str]:
    # Build agents with their respective temperature presets
    gatekeeper_agent, parameter_extractor_agent, validator_agent, summarizer_agent = build_agents()

    attempts_remaining = 3
    last_reason = None

    while attempts_remaining > 0:
        # 0) Gatekeeper: validate the user query is city-related and safe
        gate_task = make_gate_task(gatekeeper_agent, user_query)
        gate_crew = Crew(agents=[gatekeeper_agent], tasks=[gate_task], verbose=False)
        gate_output = gate_crew.kickoff()

        gate_json = gate_output.json_dict

        if not (gate_json.get("is_city_query") and gate_json.get("is_safe")):
            message = gate_json.get("returnMessageToUser") or (
                "This workflow provides city information (coords, elevation, timezone, population, postcodes) via the Open-Meteo Geocoding API. "
                "Please ask a city-focused query, e.g., 'Tell me about Paris, FR' or 'Give me details about Tokyo'."
            )
            print(message)
            return message

        # 1) Extract parameters
        extraction_agent_task = make_param_extraction_task(parameter_extractor_agent, user_query)
        extraction_crew = Crew(agents=[parameter_extractor_agent], tasks=[extraction_agent_task], verbose=False)
        parameter_extraction = extraction_crew.kickoff()

        # Extract the JSON data from the CrewOutput object
        parameter_extraction_json = parameter_extraction.json_dict
        
        # Provide defaults and minimal sanity
        params: Dict[str, Any] = {
            "name": parameter_extraction_json.get("name") or user_query,
            "count": int(parameter_extraction_json.get("count") or 10),
            "format": "json"
        }

        if parameter_extraction_json.get("language"):
            params["language"] = parameter_extraction_json["language"]
        if parameter_extraction_json.get("countryCode"):
            params["countryCode"] = parameter_extraction_json["countryCode"]

        # 2) Validate/correct parameters
        validation_task = make_validation_task(validator_agent, json.dumps(params))
        validation_crew = Crew(agents=[validator_agent], tasks=[validation_task], verbose=False)
        validation_object = validation_crew.kickoff()

        validation_json = validation_object.json_dict

        is_valid = bool(validation_json.get("valid"))
        corrected = validation_json.get("params") if isinstance(validation_json.get("params"), dict) else None
        if corrected:
            params = corrected

        if not is_valid:
            last_reason = validation_json.get("reason", "invalid parameters")
            attempts_remaining -= 1
            if attempts_remaining <= 0:
                print("Parameter validation failed after retries:", last_reason)
                return None
            # Iterate with a refined prompt on next loop
            time.sleep(0.5)
            continue

        # 3) Call the Geocoding API
        status, payload = call_geocoding_api(params)
        if status == 200 and isinstance(payload, dict) and payload.get("results"):
            # 4) Summarize
            summarize_task = make_summarize_task(summarizer_agent, json.dumps(payload), user_query)
            summarize_crew = Crew(agents=[summarizer_agent], tasks=[summarize_task], verbose=False)
            summary = summarize_crew.kickoff()
            print(str(summary))
            return str(summary)

        # If request failed or empty results, retry by refining parameters
        last_reason = payload.get("error") if isinstance(payload, dict) else "Request failed"
        attempts_remaining -= 1
        if attempts_remaining <= 0:
            print("API call failed after retries:", last_reason or f"status={status}")
            return None
        time.sleep(0.5)


if __name__ == "__main__":
    # Example simple CLI run. Replace this with your own query string.
    user_question = os.getenv("CITY_QUERY") or "Tell me about San Francisco"
    ai_response = run_city_info_workflow(user_question)


#! EXPLORE BRAINTRUST INTEGRATION...
#! https://docs.crewai.com/en/observability/overview
#! https://docs.crewai.com/en/observability/braintrust