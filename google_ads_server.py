from typing import Any, Dict, List, Optional, Union
from pydantic import Field
import os
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google.auth.transport.requests import Request
from google.auth.exceptions import RefreshError
import logging

# MCP
from mcp.server.fastmcp import FastMCP

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('google_ads_server')

mcp = FastMCP(
    "google-ads-server",
    dependencies=[
        "google-auth-oauthlib",
        "google-auth",
        "requests",
        "python-dotenv"
    ]
)

# Constants and configuration
SCOPES = ['https://www.googleapis.com/auth/adwords']
API_VERSION = "v23"  # Google Ads API version

# Load environment variables
try:
    from dotenv import load_dotenv
    # Load from .env file if it exists
    load_dotenv()
    logger.info("Environment variables loaded from .env file")
except ImportError:
    logger.warning("python-dotenv not installed, skipping .env file loading")

# Get credentials from environment variables
GOOGLE_ADS_CREDENTIALS_PATH = os.environ.get("GOOGLE_ADS_CREDENTIALS_PATH")
GOOGLE_ADS_DEVELOPER_TOKEN = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN")
GOOGLE_ADS_LOGIN_CUSTOMER_ID = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "")
GOOGLE_ADS_AUTH_TYPE = os.environ.get("GOOGLE_ADS_AUTH_TYPE", "oauth")  # oauth or service_account

def format_customer_id(customer_id: str) -> str:
    """Format customer ID to ensure it's 10 digits without dashes."""
    # Convert to string if passed as integer or another type
    customer_id = str(customer_id)
    
    # Remove any quotes surrounding the customer_id (both escaped and unescaped)
    customer_id = customer_id.replace('\"', '').replace('"', '')
    
    # Remove any non-digit characters (including dashes, braces, etc.)
    customer_id = ''.join(char for char in customer_id if char.isdigit())
    
    # Ensure it's 10 digits with leading zeros if needed
    return customer_id.zfill(10)

def get_credentials():
    """
    Get and refresh OAuth credentials or service account credentials based on the auth type.
    
    This function supports two authentication methods:
    1. OAuth 2.0 (User Authentication) - For individual users or desktop applications
    2. Service Account (Server-to-Server Authentication) - For automated systems

    Returns:
        Valid credentials object to use with Google Ads API
    """
    if not GOOGLE_ADS_CREDENTIALS_PATH:
        raise ValueError("GOOGLE_ADS_CREDENTIALS_PATH environment variable not set")
    
    auth_type = GOOGLE_ADS_AUTH_TYPE.lower()
    logger.info(f"Using authentication type: {auth_type}")
    
    # Service Account authentication
    if auth_type == "service_account":
        try:
            return get_service_account_credentials()
        except Exception as e:
            logger.error(f"Error with service account authentication: {str(e)}")
            raise
    
    # OAuth 2.0 authentication (default)
    return get_oauth_credentials()

def get_service_account_credentials():
    """Get credentials using a service account key file."""
    logger.info(f"Loading service account credentials from {GOOGLE_ADS_CREDENTIALS_PATH}")
    
    if not os.path.exists(GOOGLE_ADS_CREDENTIALS_PATH):
        raise FileNotFoundError(f"Service account key file not found at {GOOGLE_ADS_CREDENTIALS_PATH}")
    
    try:
        credentials = service_account.Credentials.from_service_account_file(
            GOOGLE_ADS_CREDENTIALS_PATH, 
            scopes=SCOPES
        )
        
        # Check if impersonation is required
        impersonation_email = os.environ.get("GOOGLE_ADS_IMPERSONATION_EMAIL")
        if impersonation_email:
            logger.info(f"Impersonating user: {impersonation_email}")
            credentials = credentials.with_subject(impersonation_email)
            
        return credentials
        
    except Exception as e:
        logger.error(f"Error loading service account credentials: {str(e)}")
        raise

def get_oauth_credentials():
    """Get and refresh OAuth user credentials."""
    creds = None
    client_config = None
    
    # Path to store the refreshed token
    token_path = GOOGLE_ADS_CREDENTIALS_PATH
    if os.path.exists(token_path) and not os.path.basename(token_path).endswith('.json'):
        # If it's not explicitly a .json file, append a default name
        token_dir = os.path.dirname(token_path)
        token_path = os.path.join(token_dir, 'google_ads_token.json')
    
    # Check if token file exists and load credentials
    if os.path.exists(token_path):
        try:
            logger.info(f"Loading OAuth credentials from {token_path}")
            with open(token_path, 'r') as f:
                creds_data = json.load(f)
                # Check if this is a client config or saved credentials
                if "installed" in creds_data or "web" in creds_data:
                    client_config = creds_data
                    logger.info("Found OAuth client configuration")
                else:
                    logger.info("Found existing OAuth token")
                    creds = Credentials.from_authorized_user_info(creds_data, SCOPES)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON in token file: {token_path}")
            creds = None
        except Exception as e:
            logger.warning(f"Error loading credentials: {str(e)}")
            creds = None
    
    # If credentials don't exist or are invalid, get new ones
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                logger.info("Refreshing expired token")
                creds.refresh(Request())
                logger.info("Token successfully refreshed")
            except RefreshError as e:
                logger.warning(f"Error refreshing token: {str(e)}, will try to get new token")
                creds = None
            except Exception as e:
                logger.error(f"Unexpected error refreshing token: {str(e)}")
                raise
        
        # If we need new credentials
        if not creds:
            # If no client_config is defined yet, create one from environment variables
            if not client_config:
                logger.info("Creating OAuth client config from environment variables")
                client_id = os.environ.get("GOOGLE_ADS_CLIENT_ID")
                client_secret = os.environ.get("GOOGLE_ADS_CLIENT_SECRET")
                
                if not client_id or not client_secret:
                    raise ValueError("GOOGLE_ADS_CLIENT_ID and GOOGLE_ADS_CLIENT_SECRET must be set if no client config file exists")
                
                client_config = {
                    "installed": {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob", "http://localhost"]
                    }
                }
            
            # Run the OAuth flow
            logger.info("Starting OAuth authentication flow")
            flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
            creds = flow.run_local_server(port=0)
            logger.info("OAuth flow completed successfully")
        
        # Save the refreshed/new credentials
        try:
            logger.info(f"Saving credentials to {token_path}")
            # Ensure directory exists
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            with open(token_path, 'w') as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.warning(f"Could not save credentials: {str(e)}")
    
    return creds

def get_headers(creds):
    """Get headers for Google Ads API requests."""
    if not GOOGLE_ADS_DEVELOPER_TOKEN:
        raise ValueError("GOOGLE_ADS_DEVELOPER_TOKEN environment variable not set")
    
    # Handle different credential types
    if isinstance(creds, service_account.Credentials):
        # For service account, we need to get a new bearer token
        auth_req = Request()
        creds.refresh(auth_req)
        token = creds.token
    else:
        # For OAuth credentials, check if token needs refresh
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    logger.info("Refreshing expired OAuth token in get_headers")
                    creds.refresh(Request())
                    logger.info("Token successfully refreshed in get_headers")
                except RefreshError as e:
                    logger.error(f"Error refreshing token in get_headers: {str(e)}")
                    raise ValueError(f"Failed to refresh OAuth token: {str(e)}")
                except Exception as e:
                    logger.error(f"Unexpected error refreshing token in get_headers: {str(e)}")
                    raise
            else:
                raise ValueError("OAuth credentials are invalid and cannot be refreshed")
        
        token = creds.token
        
    headers = {
        'Authorization': f'Bearer {token}',
        'developer-token': GOOGLE_ADS_DEVELOPER_TOKEN,
        'content-type': 'application/json'
    }
    
    if GOOGLE_ADS_LOGIN_CUSTOMER_ID:
        headers['login-customer-id'] = format_customer_id(GOOGLE_ADS_LOGIN_CUSTOMER_ID)
    
    return headers

@mcp.tool()
async def list_accounts() -> str:
    """
    Lists all accessible Google Ads accounts.
    
    This is typically the first command you should run to identify which accounts 
    you have access to. The returned account IDs can be used in subsequent commands.
    
    Returns:
        A formatted list of all Google Ads accounts accessible with your credentials
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers:listAccessibleCustomers"
        response = requests.get(url, headers=headers)
        
        if response.status_code != 200:
            return f"Error accessing accounts: {response.text}"
        
        customers = response.json()
        if not customers.get('resourceNames'):
            return "No accessible accounts found."
        
        # Format the results
        result_lines = ["Accessible Google Ads Accounts:"]
        result_lines.append("-" * 50)
        
        for resource_name in customers['resourceNames']:
            customer_id = resource_name.split('/')[-1]
            formatted_id = format_customer_id(customer_id)
            result_lines.append(f"Account ID: {formatted_id}")
        
        return "\n".join(result_lines)
    
    except Exception as e:
        return f"Error listing accounts: {str(e)}"

@mcp.tool()
async def execute_gaql_query(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    query: str = Field(description="Valid GAQL query string following Google Ads Query Language syntax")
) -> str:
    """
    Execute a custom GAQL (Google Ads Query Language) query.
    
    This tool allows you to run any valid GAQL query against the Google Ads API.
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        query: The GAQL query to execute (must follow GAQL syntax)
        
    Returns:
        Formatted query results or error message
        
    Example:
        customer_id: "1234567890"
        query: "SELECT campaign.id, campaign.name FROM campaign LIMIT 10"
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error executing query: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No results found for the query."
        
        # Format the results as a table
        result_lines = [f"Query Results for Account {formatted_customer_id}:"]
        result_lines.append("-" * 80)
        
        # Get field names from the first result
        fields = []
        first_result = results['results'][0]
        for key in first_result:
            if isinstance(first_result[key], dict):
                for subkey in first_result[key]:
                    fields.append(f"{key}.{subkey}")
            else:
                fields.append(key)
        
        # Add header
        result_lines.append(" | ".join(fields))
        result_lines.append("-" * 80)
        
        # Add data rows
        for result in results['results']:
            row_data = []
            for field in fields:
                if "." in field:
                    parent, child = field.split(".")
                    value = str(result.get(parent, {}).get(child, ""))
                else:
                    value = str(result.get(field, ""))
                row_data.append(value)
            result_lines.append(" | ".join(row_data))
        
        return "\n".join(result_lines)
    
    except Exception as e:
        return f"Error executing GAQL query: {str(e)}"

@mcp.tool()
async def get_campaign_performance(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Get campaign performance metrics for the specified time period.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to get campaign performance
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)
        
    Returns:
        Formatted table of campaign performance data
        
    Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)
        
    Example:
        customer_id: "1234567890"
        days: 14
    """
    query = f"""
        SELECT
            campaign.id,
            campaign.name,
            campaign.status,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions,
            metrics.average_cpc
        FROM campaign
        WHERE segments.date DURING LAST_{days}_DAYS
        ORDER BY metrics.cost_micros DESC
        LIMIT 50
    """
    
    return await execute_gaql_query(customer_id, query)

@mcp.tool()
async def get_ad_performance(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Get ad performance metrics for the specified time period.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to get ad performance
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)
        
    Returns:
        Formatted table of ad performance data
        
    Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)
        
    Example:
        customer_id: "1234567890"
        days: 14
    """
    query = f"""
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.status,
            campaign.name,
            ad_group.name,
            metrics.impressions,
            metrics.clicks,
            metrics.cost_micros,
            metrics.conversions
        FROM ad_group_ad
        WHERE segments.date DURING LAST_{days}_DAYS
        ORDER BY metrics.impressions DESC
        LIMIT 50
    """
    
    return await execute_gaql_query(customer_id, query)

@mcp.tool()
async def run_gaql(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    query: str = Field(description="Valid GAQL query string following Google Ads Query Language syntax"),
    format: str = Field(default="table", description="Output format: 'table', 'json', or 'csv'")
) -> str:
    """
    Execute any arbitrary GAQL (Google Ads Query Language) query with custom formatting options.
    
    This is the most powerful tool for custom Google Ads data queries.
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        query: The GAQL query to execute (any valid GAQL query)
        format: Output format ("table", "json", or "csv")
    
    Returns:
        Query results in the requested format
    
    EXAMPLE QUERIES:
    
    1. Basic campaign metrics:
        SELECT 
          campaign.name, 
          metrics.clicks, 
          metrics.impressions,
          metrics.cost_micros
        FROM campaign 
        WHERE segments.date DURING LAST_7_DAYS
    
    2. Ad group performance:
        SELECT 
          ad_group.name, 
          metrics.conversions, 
          metrics.cost_micros,
          campaign.name
        FROM ad_group 
        WHERE metrics.clicks > 100
    
    3. Keyword analysis:
        SELECT 
          keyword.text, 
          metrics.average_position, 
          metrics.ctr
        FROM keyword_view 
        ORDER BY metrics.impressions DESC
        
    4. Get conversion data:
        SELECT
          campaign.name,
          metrics.conversions,
          metrics.conversions_value,
          metrics.cost_micros
        FROM campaign
        WHERE segments.date DURING LAST_30_DAYS
        
            Note:
        Cost values are in micros (millionths) of the account currency
        (e.g., 1000000 = 1 USD in a USD account)
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error executing query: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No results found for the query."
        
        if format.lower() == "json":
            return json.dumps(results, indent=2)
        
        elif format.lower() == "csv":
            # Get field names from the first result
            fields = []
            first_result = results['results'][0]
            for key, value in first_result.items():
                if isinstance(value, dict):
                    for subkey in value:
                        fields.append(f"{key}.{subkey}")
                else:
                    fields.append(key)
            
            # Create CSV string
            csv_lines = [",".join(fields)]
            for result in results['results']:
                row_data = []
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, "")).replace(",", ";")
                    else:
                        value = str(result.get(field, "")).replace(",", ";")
                    row_data.append(value)
                csv_lines.append(",".join(row_data))
            
            return "\n".join(csv_lines)
        
        else:  # default table format
            result_lines = [f"Query Results for Account {formatted_customer_id}:"]
            result_lines.append("-" * 100)
            
            # Get field names and maximum widths
            fields = []
            field_widths = {}
            first_result = results['results'][0]
            
            for key, value in first_result.items():
                if isinstance(value, dict):
                    for subkey in value:
                        field = f"{key}.{subkey}"
                        fields.append(field)
                        field_widths[field] = len(field)
                else:
                    fields.append(key)
                    field_widths[key] = len(key)
            
            # Calculate maximum field widths
            for result in results['results']:
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, ""))
                    else:
                        value = str(result.get(field, ""))
                    field_widths[field] = max(field_widths[field], len(value))
            
            # Create formatted header
            header = " | ".join(f"{field:{field_widths[field]}}" for field in fields)
            result_lines.append(header)
            result_lines.append("-" * len(header))
            
            # Add data rows
            for result in results['results']:
                row_data = []
                for field in fields:
                    if "." in field:
                        parent, child = field.split(".")
                        value = str(result.get(parent, {}).get(child, ""))
                    else:
                        value = str(result.get(field, ""))
                    row_data.append(f"{value:{field_widths[field]}}")
                result_lines.append(" | ".join(row_data))
            
            return "\n".join(result_lines)
    
    except Exception as e:
        return f"Error executing GAQL query: {str(e)}"

@mcp.tool()
async def get_ad_creatives(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'")
) -> str:
    """
    Get ad creative details including headlines, descriptions, and URLs.
    
    This tool retrieves the actual ad content (headlines, descriptions) 
    for review and analysis. Great for creative audits.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run this command with the desired account ID
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        
    Returns:
        Formatted list of ad creative details
        
    Example:
        customer_id: "1234567890"
    """
    query = """
        SELECT
            ad_group_ad.ad.id,
            ad_group_ad.ad.name,
            ad_group_ad.ad.type,
            ad_group_ad.ad.final_urls,
            ad_group_ad.status,
            ad_group_ad.ad.responsive_search_ad.headlines,
            ad_group_ad.ad.responsive_search_ad.descriptions,
            ad_group.name,
            campaign.name
        FROM ad_group_ad
        WHERE ad_group_ad.status != 'REMOVED'
        ORDER BY campaign.name, ad_group.name
        LIMIT 50
    """
    
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error retrieving ad creatives: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No ad creatives found for this customer ID."
        
        # Format the results in a readable way
        output_lines = [f"Ad Creatives for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)
        
        for i, result in enumerate(results['results'], 1):
            ad = result.get('adGroupAd', {}).get('ad', {})
            ad_group = result.get('adGroup', {})
            campaign = result.get('campaign', {})
            
            output_lines.append(f"\n{i}. Campaign: {campaign.get('name', 'N/A')}")
            output_lines.append(f"   Ad Group: {ad_group.get('name', 'N/A')}")
            output_lines.append(f"   Ad ID: {ad.get('id', 'N/A')}")
            output_lines.append(f"   Ad Name: {ad.get('name', 'N/A')}")
            output_lines.append(f"   Status: {result.get('adGroupAd', {}).get('status', 'N/A')}")
            output_lines.append(f"   Type: {ad.get('type', 'N/A')}")
            
            # Handle Responsive Search Ads
            rsa = ad.get('responsiveSearchAd', {})
            if rsa:
                if 'headlines' in rsa:
                    output_lines.append("   Headlines:")
                    for headline in rsa['headlines']:
                        output_lines.append(f"     - {headline.get('text', 'N/A')}")
                
                if 'descriptions' in rsa:
                    output_lines.append("   Descriptions:")
                    for desc in rsa['descriptions']:
                        output_lines.append(f"     - {desc.get('text', 'N/A')}")
            
            # Handle Final URLs
            final_urls = ad.get('finalUrls', [])
            if final_urls:
                output_lines.append(f"   Final URLs: {', '.join(final_urls)}")
            
            output_lines.append("-" * 80)
        
        return "\n".join(output_lines)
    
    except Exception as e:
        return f"Error retrieving ad creatives: {str(e)}"

@mcp.tool()
async def get_account_currency(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'")
) -> str:
    """
    Retrieve the default currency code used by the Google Ads account.
    
    IMPORTANT: Run this first before analyzing cost data to understand which currency
    the account uses. Cost values are always displayed in the account's currency.
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
    
    Returns:
        The account's default currency code (e.g., 'USD', 'EUR', 'GBP')
        
    Example:
        customer_id: "1234567890"
    """
    query = """
        SELECT
            customer.id,
            customer.currency_code
        FROM customer
        LIMIT 1
    """
    
    try:
        creds = get_credentials()
        
        # Force refresh if needed
        if not creds.valid:
            logger.info("Credentials not valid, attempting refresh...")
            if hasattr(creds, 'refresh_token') and creds.refresh_token:
                creds.refresh(Request())
                logger.info("Credentials refreshed successfully")
            else:
                raise ValueError("Invalid credentials and no refresh token available")
        
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error retrieving account currency: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No account information found for this customer ID."
        
        # Extract the currency code from the results
        customer = results['results'][0].get('customer', {})
        currency_code = customer.get('currencyCode', 'Not specified')
        
        return f"Account {formatted_customer_id} uses currency: {currency_code}"
    
    except Exception as e:
        logger.error(f"Error retrieving account currency: {str(e)}")
        return f"Error retrieving account currency: {str(e)}"

@mcp.resource("gaql://reference")
def gaql_reference() -> str:
    """Google Ads Query Language (GAQL) reference documentation."""
    return """
    # Google Ads Query Language (GAQL) Reference
    
    GAQL is similar to SQL but with specific syntax for Google Ads. Here's a quick reference:
    
    ## Basic Query Structure
    ```
    SELECT field1, field2, ... 
    FROM resource_type
    WHERE condition
    ORDER BY field [ASC|DESC]
    LIMIT n
    ```
    
    ## Common Field Types
    
    ### Resource Fields
    - campaign.id, campaign.name, campaign.status
    - ad_group.id, ad_group.name, ad_group.status
    - ad_group_ad.ad.id, ad_group_ad.ad.final_urls
    - keyword.text, keyword.match_type
    
    ### Metric Fields
    - metrics.impressions
    - metrics.clicks
    - metrics.cost_micros
    - metrics.conversions
    - metrics.ctr
    - metrics.average_cpc
    
    ### Segment Fields
    - segments.date
    - segments.device
    - segments.day_of_week
    
    ## Common WHERE Clauses
    
    ### Date Ranges
    - WHERE segments.date DURING LAST_7_DAYS
    - WHERE segments.date DURING LAST_30_DAYS
    - WHERE segments.date BETWEEN '2023-01-01' AND '2023-01-31'
    
    ### Filtering
    - WHERE campaign.status = 'ENABLED'
    - WHERE metrics.clicks > 100
    - WHERE campaign.name LIKE '%Brand%'
    
    ## Tips
    - Always check account currency before analyzing cost data
    - Cost values are in micros (millionths): 1000000 = 1 unit of currency
    - Use LIMIT to avoid large result sets
    """

@mcp.prompt("google_ads_workflow")
def google_ads_workflow() -> str:
    """Provides guidance on the recommended workflow for using Google Ads tools."""
    return """
    I'll help you analyze your Google Ads account data. Here's the recommended workflow:
    
    1. First, let's list all the accounts you have access to:
       - Run the `list_accounts()` tool to get available account IDs
    
    2. Before analyzing cost data, let's check which currency the account uses:
       - Run `get_account_currency(customer_id="ACCOUNT_ID")` with your selected account
    
    3. Now we can explore the account data:
       - For campaign performance: `get_campaign_performance(customer_id="ACCOUNT_ID", days=30)`
       - For ad performance: `get_ad_performance(customer_id="ACCOUNT_ID", days=30)`
       - For ad creative review: `get_ad_creatives(customer_id="ACCOUNT_ID")`
    
    4. For custom queries, use the GAQL query tool:
       - `run_gaql(customer_id="ACCOUNT_ID", query="YOUR_QUERY", format="table")`
    
    5. Let me know if you have specific questions about:
       - Campaign performance
       - Ad performance
       - Keywords
       - Budgets
       - Conversions
    
    Important: Always provide the customer_id as a string.
    For example: customer_id="1234567890"
    """

@mcp.prompt("gaql_help")
def gaql_help() -> str:
    """Provides assistance for writing GAQL queries."""
    return """
    I'll help you write a Google Ads Query Language (GAQL) query. Here are some examples to get you started:
    
    ## Get campaign performance last 30 days
    ```
    SELECT
      campaign.id,
      campaign.name,
      campaign.status,
      metrics.impressions,
      metrics.clicks,
      metrics.cost_micros,
      metrics.conversions
    FROM campaign
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY metrics.cost_micros DESC
    ```
    
    ## Get keyword performance
    ```
    SELECT
      keyword.text,
      keyword.match_type,
      metrics.impressions,
      metrics.clicks,
      metrics.cost_micros,
      metrics.conversions
    FROM keyword_view
    WHERE segments.date DURING LAST_30_DAYS
    ORDER BY metrics.clicks DESC
    ```
    
    ## Get ads with poor performance
    ```
    SELECT
      ad_group_ad.ad.id,
      ad_group_ad.ad.name,
      campaign.name,
      ad_group.name,
      metrics.impressions,
      metrics.clicks,
      metrics.conversions
    FROM ad_group_ad
    WHERE 
      segments.date DURING LAST_30_DAYS
      AND metrics.impressions > 1000
      AND metrics.ctr < 0.01
    ORDER BY metrics.impressions DESC
    ```
    
    Once you've chosen a query, use it with:
    ```
    run_gaql(customer_id="YOUR_ACCOUNT_ID", query="YOUR_QUERY_HERE")
    ```
    
    Remember:
    - Always provide the customer_id as a string
    - Cost values are in micros (1,000,000 = 1 unit of currency)
    - Use LIMIT to avoid large result sets
    - Check the account currency before analyzing cost data
    """

@mcp.tool()
async def get_image_assets(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    limit: int = Field(default=50, description="Maximum number of image assets to return")
) -> str:
    """
    Retrieve all image assets in the account including their full-size URLs.
    
    This tool allows you to get details about image assets used in your Google Ads account,
    including the URLs to download the full-size images for further processing or analysis.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run this command with the desired account ID
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        limit: Maximum number of image assets to return (default: 50)
        
    Returns:
        Formatted list of image assets with their download URLs
        
    Example:
        customer_id: "1234567890"
        limit: 100
    """
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type,
            asset.image_asset.full_size.url,
            asset.image_asset.full_size.height_pixels,
            asset.image_asset.full_size.width_pixels,
            asset.image_asset.file_size
        FROM
            asset
        WHERE
            asset.type = 'IMAGE'
        LIMIT {limit}
    """
    
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error retrieving image assets: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No image assets found for this customer ID."
        
        # Format the results in a readable way
        output_lines = [f"Image Assets for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)
        
        for i, result in enumerate(results['results'], 1):
            asset = result.get('asset', {})
            image_asset = asset.get('imageAsset', {})
            full_size = image_asset.get('fullSize', {})
            
            output_lines.append(f"\n{i}. Asset ID: {asset.get('id', 'N/A')}")
            output_lines.append(f"   Name: {asset.get('name', 'N/A')}")
            
            if full_size:
                output_lines.append(f"   Image URL: {full_size.get('url', 'N/A')}")
                output_lines.append(f"   Dimensions: {full_size.get('widthPixels', 'N/A')} x {full_size.get('heightPixels', 'N/A')} px")
            
            file_size = image_asset.get('fileSize', 'N/A')
            if file_size != 'N/A':
                # Convert to KB for readability
                file_size_kb = int(file_size) / 1024
                output_lines.append(f"   File Size: {file_size_kb:.2f} KB")
            
            output_lines.append("-" * 80)
        
        return "\n".join(output_lines)
    
    except Exception as e:
        return f"Error retrieving image assets: {str(e)}"

@mcp.tool()
async def download_image_asset(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    asset_id: str = Field(description="The ID of the image asset to download"),
    output_dir: str = Field(default="./ad_images", description="Directory to save the downloaded image")
) -> str:
    """
    Download a specific image asset from a Google Ads account.
    
    This tool allows you to download the full-size version of an image asset
    for further processing, analysis, or backup.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_image_assets() to get available image asset IDs
    3. Finally use this command to download specific images
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        asset_id: The ID of the image asset to download
        output_dir: Directory where the image should be saved (default: ./ad_images)
        
    Returns:
        Status message indicating success or failure of the download
        
    Example:
        customer_id: "1234567890"
        asset_id: "12345"
        output_dir: "./my_ad_images"
    """
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.image_asset.full_size.url
        FROM
            asset
        WHERE
            asset.type = 'IMAGE'
            AND asset.id = {asset_id}
        LIMIT 1
    """
    
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error retrieving image asset: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return f"No image asset found with ID {asset_id}"
        
        # Extract the image URL
        asset = results['results'][0].get('asset', {})
        image_url = asset.get('imageAsset', {}).get('fullSize', {}).get('url')
        asset_name = asset.get('name', f"image_{asset_id}")
        
        if not image_url:
            return f"No download URL found for image asset ID {asset_id}"
        
        # Validate and sanitize the output directory to prevent path traversal
        try:
            # Get the base directory (current working directory)
            base_dir = Path.cwd()
            # Resolve the output directory to an absolute path
            resolved_output_dir = Path(output_dir).resolve()
            
            # Ensure the resolved path is within or under the current working directory
            # This prevents path traversal attacks like "../../../etc"
            try:
                resolved_output_dir.relative_to(base_dir)
            except ValueError:
                # If the path is not relative to base_dir, use the default safe directory
                resolved_output_dir = base_dir / "ad_images"
                logger.warning(f"Invalid output directory '{output_dir}' - using default './ad_images'")
            
            # Create output directory if it doesn't exist
            resolved_output_dir.mkdir(parents=True, exist_ok=True)
            
        except Exception as e:
            return f"Error creating output directory: {str(e)}"
        
        # Download the image
        image_response = requests.get(image_url)
        if image_response.status_code != 200:
            return f"Failed to download image: HTTP {image_response.status_code}"
        
        # Clean the filename to be safe for filesystem
        safe_name = ''.join(c for c in asset_name if c.isalnum() or c in ' ._-')
        filename = f"{asset_id}_{safe_name}.jpg"
        file_path = resolved_output_dir / filename
        
        # Save the image
        with open(file_path, 'wb') as f:
            f.write(image_response.content)
        
        return f"Successfully downloaded image asset {asset_id} to {file_path}"
    
    except Exception as e:
        return f"Error downloading image asset: {str(e)}"

@mcp.tool()
async def get_asset_usage(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    asset_id: str = Field(default=None, description="Optional: specific asset ID to look up (leave empty to get all image assets)"),
    asset_type: str = Field(default="IMAGE", description="Asset type to search for ('IMAGE', 'TEXT', 'VIDEO', etc.)")
) -> str:
    """
    Find where specific assets are being used in campaigns, ad groups, and ads.
    
    This tool helps you analyze how assets are linked to campaigns and ads across your account,
    which is useful for creative analysis and optimization.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Run get_image_assets() to see available assets
    3. Use this command to see where specific assets are used
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        asset_id: Optional specific asset ID to look up (leave empty to get all assets of the specified type)
        asset_type: Type of asset to search for (default: 'IMAGE')
        
    Returns:
        Formatted report showing where assets are used in the account
        
    Example:
        customer_id: "1234567890"
        asset_id: "12345"
        asset_type: "IMAGE"
    """
    # Build the query based on whether a specific asset ID was provided
    where_clause = f"asset.type = '{asset_type}'"
    if asset_id:
        where_clause += f" AND asset.id = {asset_id}"
    
    # First get the assets themselves
    assets_query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.type
        FROM
            asset
        WHERE
            {where_clause}
        LIMIT 100
    """
    
    # Then get the associations between assets and campaigns/ad groups
    # Try using campaign_asset instead of asset_link
    associations_query = f"""
        SELECT
            campaign.id,
            campaign.name,
            asset.id,
            asset.name,
            asset.type
        FROM
            campaign_asset
        WHERE
            {where_clause}
        LIMIT 500
    """

    # Also try ad_group_asset for ad group level information
    ad_group_query = f"""
        SELECT
            ad_group.id,
            ad_group.name,
            asset.id,
            asset.name,
            asset.type
        FROM
            ad_group_asset
        WHERE
            {where_clause}
        LIMIT 500
    """
    
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        
        # First get the assets
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        payload = {"query": assets_query}
        assets_response = requests.post(url, headers=headers, json=payload)
        
        if assets_response.status_code != 200:
            return f"Error retrieving assets: {assets_response.text}"
        
        assets_results = assets_response.json()
        if not assets_results.get('results'):
            return f"No {asset_type} assets found for this customer ID."
        
        # Now get the associations
        payload = {"query": associations_query}
        assoc_response = requests.post(url, headers=headers, json=payload)
        
        if assoc_response.status_code != 200:
            return f"Error retrieving asset associations: {assoc_response.text}"
        
        assoc_results = assoc_response.json()
        
        # Format the results in a readable way
        output_lines = [f"Asset Usage for Customer ID {formatted_customer_id}:"]
        output_lines.append("=" * 80)
        
        # Create a dictionary to organize asset usage by asset ID
        asset_usage = {}
        
        # Initialize the asset usage dictionary with basic asset info
        for result in assets_results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')
            if asset_id:
                asset_usage[asset_id] = {
                    'name': asset.get('name', 'Unnamed asset'),
                    'type': asset.get('type', 'Unknown'),
                    'usage': []
                }
        
        # Add usage information from the associations
        for result in assoc_results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')
            
            if asset_id and asset_id in asset_usage:
                campaign = result.get('campaign', {})
                ad_group = result.get('adGroup', {})
                ad = result.get('adGroupAd', {}).get('ad', {}) if 'adGroupAd' in result else {}
                asset_link = result.get('assetLink', {})
                
                usage_info = {
                    'campaign_id': campaign.get('id', 'N/A'),
                    'campaign_name': campaign.get('name', 'N/A'),
                    'ad_group_id': ad_group.get('id', 'N/A'),
                    'ad_group_name': ad_group.get('name', 'N/A'),
                    'ad_id': ad.get('id', 'N/A') if ad else 'N/A',
                    'ad_name': ad.get('name', 'N/A') if ad else 'N/A'
                }
                
                asset_usage[asset_id]['usage'].append(usage_info)
        
        # Format the output
        for asset_id, info in asset_usage.items():
            output_lines.append(f"\nAsset ID: {asset_id}")
            output_lines.append(f"Name: {info['name']}")
            output_lines.append(f"Type: {info['type']}")
            
            if info['usage']:
                output_lines.append("\nUsed in:")
                output_lines.append("-" * 60)
                output_lines.append(f"{'Campaign':<30} | {'Ad Group':<30}")
                output_lines.append("-" * 60)
                
                for usage in info['usage']:
                    campaign_str = f"{usage['campaign_name']} ({usage['campaign_id']})"
                    ad_group_str = f"{usage['ad_group_name']} ({usage['ad_group_id']})"
                    
                    output_lines.append(f"{campaign_str[:30]:<30} | {ad_group_str[:30]:<30}")
            
            output_lines.append("=" * 80)
        
        return "\n".join(output_lines)
    
    except Exception as e:
        return f"Error retrieving asset usage: {str(e)}"

@mcp.tool()
async def analyze_image_assets(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'"),
    days: int = Field(default=30, description="Number of days to look back (7, 30, 90, etc.)")
) -> str:
    """
    Analyze image assets with their performance metrics across campaigns.
    
    This comprehensive tool helps you understand which image assets are performing well
    by showing metrics like impressions, clicks, and conversions for each image.
    
    RECOMMENDED WORKFLOW:
    1. First run list_accounts() to get available account IDs
    2. Then run get_account_currency() to see what currency the account uses
    3. Finally run this command to analyze image asset performance
    
    Args:
        customer_id: The Google Ads customer ID as a string (10 digits, no dashes)
        days: Number of days to look back (default: 30)
        
    Returns:
        Detailed report of image assets and their performance metrics
        
    Example:
        customer_id: "1234567890"
        days: 14
    """
    # Make sure to use a valid date range format
    # Valid formats are: LAST_7_DAYS, LAST_14_DAYS, LAST_30_DAYS, etc. (with underscores)
    if days == 7:
        date_range = "LAST_7_DAYS"
    elif days == 14:
        date_range = "LAST_14_DAYS"
    elif days == 30:
        date_range = "LAST_30_DAYS"
    else:
        # Default to 30 days if not a standard range
        date_range = "LAST_30_DAYS"
        
    query = f"""
        SELECT
            asset.id,
            asset.name,
            asset.image_asset.full_size.url,
            asset.image_asset.full_size.width_pixels,
            asset.image_asset.full_size.height_pixels,
            campaign.name,
            metrics.impressions,
            metrics.clicks,
            metrics.conversions,
            metrics.cost_micros
        FROM
            campaign_asset
        WHERE
            asset.type = 'IMAGE'
            AND segments.date DURING LAST_30_DAYS
        ORDER BY
            metrics.impressions DESC
        LIMIT 200
    """
    
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        
        formatted_customer_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_customer_id}/googleAds:search"
        
        payload = {"query": query}
        response = requests.post(url, headers=headers, json=payload)
        
        if response.status_code != 200:
            return f"Error analyzing image assets: {response.text}"
        
        results = response.json()
        if not results.get('results'):
            return "No image asset performance data found for this customer ID and time period."
        
        # Group results by asset ID
        assets_data = {}
        for result in results.get('results', []):
            asset = result.get('asset', {})
            asset_id = asset.get('id')
            
            if asset_id not in assets_data:
                assets_data[asset_id] = {
                    'name': asset.get('name', f"Asset {asset_id}"),
                    'url': asset.get('imageAsset', {}).get('fullSize', {}).get('url', 'N/A'),
                    'dimensions': f"{asset.get('imageAsset', {}).get('fullSize', {}).get('widthPixels', 'N/A')} x {asset.get('imageAsset', {}).get('fullSize', {}).get('heightPixels', 'N/A')}",
                    'impressions': 0,
                    'clicks': 0,
                    'conversions': 0,
                    'cost_micros': 0,
                    'campaigns': set(),
                    'ad_groups': set()
                }
            
            # Aggregate metrics
            metrics = result.get('metrics', {})
            assets_data[asset_id]['impressions'] += int(metrics.get('impressions', 0))
            assets_data[asset_id]['clicks'] += int(metrics.get('clicks', 0))
            assets_data[asset_id]['conversions'] += float(metrics.get('conversions', 0))
            assets_data[asset_id]['cost_micros'] += int(metrics.get('costMicros', 0))
            
            # Add campaign and ad group info
            campaign = result.get('campaign', {})
            ad_group = result.get('adGroup', {})
            
            if campaign.get('name'):
                assets_data[asset_id]['campaigns'].add(campaign.get('name'))
            if ad_group.get('name'):
                assets_data[asset_id]['ad_groups'].add(ad_group.get('name'))
        
        # Format the results
        output_lines = [f"Image Asset Performance Analysis for Customer ID {formatted_customer_id} (Last {days} days):"]
        output_lines.append("=" * 100)
        
        # Sort assets by impressions (highest first)
        sorted_assets = sorted(assets_data.items(), key=lambda x: x[1]['impressions'], reverse=True)
        
        for asset_id, data in sorted_assets:
            output_lines.append(f"\nAsset ID: {asset_id}")
            output_lines.append(f"Name: {data['name']}")
            output_lines.append(f"Dimensions: {data['dimensions']}")
            
            # Calculate CTR if there are impressions
            ctr = (data['clicks'] / data['impressions'] * 100) if data['impressions'] > 0 else 0
            
            # Format metrics
            output_lines.append(f"\nPerformance Metrics:")
            output_lines.append(f"  Impressions: {data['impressions']:,}")
            output_lines.append(f"  Clicks: {data['clicks']:,}")
            output_lines.append(f"  CTR: {ctr:.2f}%")
            output_lines.append(f"  Conversions: {data['conversions']:.2f}")
            output_lines.append(f"  Cost (micros): {data['cost_micros']:,}")
            
            # Show where it's used
            output_lines.append(f"\nUsed in {len(data['campaigns'])} campaigns:")
            for campaign in list(data['campaigns'])[:5]:  # Show first 5 campaigns
                output_lines.append(f"  - {campaign}")
            if len(data['campaigns']) > 5:
                output_lines.append(f"  - ... and {len(data['campaigns']) - 5} more")
            
            # Add URL
            if data['url'] != 'N/A':
                output_lines.append(f"\nImage URL: {data['url']}")
            
            output_lines.append("-" * 100)
        
        return "\n".join(output_lines)
    
    except Exception as e:
        return f"Error analyzing image assets: {str(e)}"

@mcp.tool()
async def list_resources(
    customer_id: str = Field(description="Google Ads customer ID (10 digits, no dashes). Example: '9873186703'")
) -> str:
    """
    List valid resources that can be used in GAQL FROM clauses.
    
    Args:
        customer_id: The Google Ads customer ID as a string
        
    Returns:
        Formatted list of valid resources
    """
    # Example query that lists some common resources
    # This might need to be adjusted based on what's available in your API version
    query = """
        SELECT
            google_ads_field.name,
            google_ads_field.category,
            google_ads_field.data_type
        FROM
            google_ads_field
        WHERE
            google_ads_field.category = 'RESOURCE'
        ORDER BY
            google_ads_field.name
    """
    
    # Use your existing run_gaql function to execute this query
    return await run_gaql(customer_id, query)

@mcp.tool()
async def create_campaign_budget(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    name: str = Field(..., description="Budget name"),
    amount_micros: int = Field(..., description="Daily budget in micros (e.g. 5000000 = 5 USD)"),
) -> str:
    """
    Create a campaign budget. Returns the budget resource name for use in create_search_campaign.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        name: Budget name (e.g. 'Filtrix Daily Budget')
        amount_micros: Daily budget in micros — 1 USD = 1000000. Example: 5000000 = $5/day
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignBudgets:mutate"
        payload = {
            "operations": [
                {
                    "create": {
                        "name": name,
                        "deliveryMethod": "STANDARD",
                        "amountMicros": str(amount_micros)
                    }
                }
            ]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error creating budget: {json.dumps(data, indent=2)}"

        resource_name = data["results"][0]["resourceName"]
        return f"Budget created successfully.\nResource name: {resource_name}\nUse this resource name in create_search_campaign."

    except Exception as e:
        logger.error(f"Error in create_campaign_budget: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def create_search_campaign(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    name: str = Field(..., description="Campaign name"),
    budget_resource_name: str = Field(..., description="Budget resource name from create_campaign_budget"),
    target_google_search: bool = Field(True, description="Target Google Search"),
    target_search_network: bool = Field(True, description="Target Search Partner Network"),
    target_content_network: bool = Field(False, description="Target Display Network"),
) -> str:
    """
    Create a Search campaign (PAUSED by default for safety review before activation).

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        name: Campaign name (e.g. 'Filtrix - Brand Search')
        budget_resource_name: Resource name from create_campaign_budget (e.g. customers/123/campaignBudgets/456)
        target_google_search: Show ads on Google Search (default True)
        target_search_network: Show ads on Search Partner sites (default True)
        target_content_network: Show ads on Display Network (default False)
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaigns:mutate"
        payload = {
            "operations": [
                {
                    "create": {
                        "name": name,
                        "status": "PAUSED",
                        "advertisingChannelType": "SEARCH",
                        "campaignBudget": budget_resource_name,
                        "manualCpc": {},
                        "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
                        "networkSettings": {
                            "targetGoogleSearch": target_google_search,
                            "targetSearchNetwork": target_search_network,
                            "targetContentNetwork": target_content_network,
                            "targetPartnerSearchNetwork": False
                        }
                    }
                }
            ]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error creating campaign: {json.dumps(data, indent=2)}"

        resource_name = data["results"][0]["resourceName"]
        return (
            f"Campaign '{name}' created successfully (status: PAUSED).\n"
            f"Resource name: {resource_name}\n"
            f"Next step: create an ad group using create_ad_group with this resource name."
        )

    except Exception as e:
        logger.error(f"Error in create_search_campaign: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def create_ad_group(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_resource_name: str = Field(..., description="Campaign resource name from create_search_campaign"),
    name: str = Field(..., description="Ad group name"),
    cpc_bid_micros: int = Field(1000000, description="Max CPC bid in micros (default 1000000 = $1)"),
) -> str:
    """
    Create an ad group inside a Search campaign.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        campaign_resource_name: Resource name from create_search_campaign
        name: Ad group name (e.g. 'Stock Screener - Exact')
        cpc_bid_micros: Max CPC bid in micros — 1 USD = 1000000 (default $1)
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/adGroups:mutate"
        payload = {
            "operations": [
                {
                    "create": {
                        "name": name,
                        "campaign": campaign_resource_name,
                        "status": "ENABLED",
                        "type": "SEARCH_STANDARD",
                        "cpcBidMicros": str(cpc_bid_micros)
                    }
                }
            ]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error creating ad group: {json.dumps(data, indent=2)}"

        resource_name = data["results"][0]["resourceName"]
        return (
            f"Ad group '{name}' created successfully.\n"
            f"Resource name: {resource_name}\n"
            f"Next steps:\n"
            f"  1. Add keywords: create_keywords\n"
            f"  2. Add ad: create_responsive_search_ad"
        )

    except Exception as e:
        logger.error(f"Error in create_ad_group: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def create_keywords(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    ad_group_resource_name: str = Field(..., description="Ad group resource name from create_ad_group"),
    keywords: str = Field(..., description="Comma-separated keywords (e.g. 'stock screener, trade ideas alternative, backtesting tool')"),
    match_type: str = Field("EXACT", description="Match type: EXACT, PHRASE, or BROAD"),
) -> str:
    """
    Add keywords to an ad group.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        ad_group_resource_name: Resource name from create_ad_group
        keywords: Comma-separated list of keywords
        match_type: EXACT (default), PHRASE, or BROAD
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        keyword_list = [k.strip() for k in keywords.split(",") if k.strip()]
        if not keyword_list:
            return "Error: no keywords provided."

        valid_match_types = {"EXACT", "PHRASE", "BROAD"}
        match_type = match_type.upper()
        if match_type not in valid_match_types:
            return f"Error: match_type must be one of {valid_match_types}"

        operations = [
            {
                "create": {
                    "adGroup": ad_group_resource_name,
                    "status": "ENABLED",
                    "keyword": {
                        "text": kw,
                        "matchType": match_type
                    }
                }
            }
            for kw in keyword_list
        ]

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/adGroupCriteria:mutate"
        payload = {"operations": operations}
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error creating keywords: {json.dumps(data, indent=2)}"

        added = [r["resourceName"] for r in data.get("results", [])]
        return (
            f"Added {len(added)} keyword(s) ({match_type}) to ad group:\n"
            + "\n".join(f"  • {kw}" for kw in keyword_list)
        )

    except Exception as e:
        logger.error(f"Error in create_keywords: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def create_responsive_search_ad(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    ad_group_resource_name: str = Field(..., description="Ad group resource name from create_ad_group"),
    headlines: str = Field(..., description="Pipe-separated headlines, 3–15 items, max 30 chars each (e.g. 'Stock Screener|Find Top Stocks|Backtest Any Strategy')"),
    descriptions: str = Field(..., description="Pipe-separated descriptions, 2–4 items, max 90 chars each"),
    final_url: str = Field(..., description="Landing page URL (e.g. https://filtrix.net)"),
) -> str:
    """
    Create a Responsive Search Ad (RSA) — the standard Google Search ad format.
    Google automatically tests combinations of your headlines and descriptions.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        ad_group_resource_name: Resource name from create_ad_group
        headlines: Pipe-separated, 3–15 headlines, max 30 chars each
        descriptions: Pipe-separated, 2–4 descriptions, max 90 chars each
        final_url: Landing page URL
    """
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        headline_list = [h.strip() for h in headlines.split("|") if h.strip()]
        desc_list = [d.strip() for d in descriptions.split("|") if d.strip()]

        if len(headline_list) < 3:
            return "Error: at least 3 headlines required."
        if len(headline_list) > 15:
            return "Error: maximum 15 headlines allowed."
        if len(desc_list) < 2:
            return "Error: at least 2 descriptions required."
        if len(desc_list) > 4:
            return "Error: maximum 4 descriptions allowed."

        for h in headline_list:
            if len(h) > 30:
                return f"Error: headline too long (max 30 chars): '{h}' ({len(h)} chars)"
        for d in desc_list:
            if len(d) > 90:
                return f"Error: description too long (max 90 chars): '{d}' ({len(d)} chars)"

        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/adGroupAds:mutate"
        payload = {
            "operations": [
                {
                    "create": {
                        "adGroup": ad_group_resource_name,
                        "status": "ENABLED",
                        "ad": {
                            "finalUrls": [final_url],
                            "responsiveSearchAd": {
                                "headlines": [{"text": h} for h in headline_list],
                                "descriptions": [{"text": d} for d in desc_list]
                            }
                        }
                    }
                }
            ]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error creating ad: {json.dumps(data, indent=2)}"

        resource_name = data["results"][0]["resourceName"]
        return (
            f"Responsive Search Ad created successfully.\n"
            f"Resource name: {resource_name}\n"
            f"Headlines ({len(headline_list)}): {', '.join(headline_list)}\n"
            f"Descriptions ({len(desc_list)}): {', '.join(desc_list)}\n"
            f"Landing page: {final_url}\n\n"
            f"Campaign is PAUSED — enable it in Google Ads UI when ready to go live."
        )

    except Exception as e:
        logger.error(f"Error in create_responsive_search_ad: {e}")
        return f"Error: {str(e)}"


# ISO 3166-1 alpha-2 → Google Ads geoTargetConstant ID
# https://developers.google.com/google-ads/api/reference/data/geotargets
GEO_TARGET_CONSTANTS = {
    "US": "2840", "CA": "2124", "GB": "2826", "UK": "2826", "AU": "2036",
    "IE": "2372", "SG": "2702", "NZ": "2554", "DE": "2276", "FR": "2250",
    "ES": "2724", "IT": "2380", "NL": "2528", "SE": "2752", "NO": "2578",
    "DK": "2208", "FI": "2246", "PL": "2616", "JP": "2392", "CH": "2756",
    "AT": "2040", "BE": "2056", "PT": "2620", "MX": "2484", "BR": "2076",
    "IN": "2356", "HK": "2344", "ZA": "2710", "AE": "2784", "IL": "2376",
}

# Google Ads language constant IDs
# https://developers.google.com/google-ads/api/data/codes-formats#languages
LANGUAGE_CONSTANTS = {
    "en": "1000", "de": "1001", "fr": "1002", "es": "1003", "it": "1004",
    "ja": "1005", "nl": "1010", "pt": "1014", "sv": "1015", "ru": "1031",
    "zh": "1017", "pl": "1030", "tr": "1037", "ar": "1019", "uk": "1036",
}


@mcp.tool()
async def update_campaign_status(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part — e.g. 23773808819)"),
    status: str = Field(..., description="New status: ENABLED, PAUSED, or REMOVED"),
) -> str:
    """
    Pause, enable, or remove a campaign.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        campaign_id: Campaign ID
        status: ENABLED | PAUSED | REMOVED
    """
    status = status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        return "Error: status must be ENABLED, PAUSED, or REMOVED."
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        resource_name = f"customers/{formatted_id}/campaigns/{campaign_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaigns:mutate"
        payload = {
            "operations": [{
                "update": {"resourceName": resource_name, "status": status},
                "updateMask": "status",
            }]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error updating campaign: {json.dumps(data, indent=2)}"
        return f"Campaign {campaign_id} status set to {status}."
    except Exception as e:
        logger.error(f"Error in update_campaign_status: {e}")
        return f"Error: {str(e)}"


def _resolve_geo_targets(targets: str) -> tuple[list[tuple[str, str]], list[str]]:
    """
    Parse a comma-separated list of geo targets into (label, geo_constant_id) pairs.
    Each token is either an ISO country code from GEO_TARGET_CONSTANTS, or a raw
    numeric geo target constant ID (e.g. 1005407 for Barcelona city).
    Returns (resolved_pairs, unknown_tokens).
    """
    resolved: list[tuple[str, str]] = []
    unknown: list[str] = []
    for raw in targets.split(","):
        token = raw.strip()
        if not token:
            continue
        if token.isdigit():
            resolved.append((token, token))
        else:
            code = token.upper()
            if code in GEO_TARGET_CONSTANTS:
                resolved.append((code, GEO_TARGET_CONSTANTS[code]))
            else:
                unknown.append(token)
    return resolved, unknown


@mcp.tool()
async def add_campaign_geo_targets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    country_codes: str = Field(..., description="Comma-separated ISO country codes (e.g. 'US,CA,GB') OR raw geo target constant IDs (e.g. '1005407' for Barcelona city). Mixed lists are OK: 'US,PT,1005407'."),
) -> str:
    """
    Add geographic location targets to a campaign.

    Accepts ISO country codes from the built-in map (US, CA, GB, AU, IE, SG, NZ, DE,
    FR, ES, IT, PL, PT, etc.) AND/OR raw numeric Google Ads geo target constant IDs
    for sub-country targeting (cities, regions, DMAs). Look up constant IDs at
    https://developers.google.com/google-ads/api/data/geotargets — e.g. 1005407 =
    Barcelona, 1023191 = New York.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        country_codes: Comma-separated ISO codes and/or numeric geo target constant IDs
    """
    try:
        resolved, unknown = _resolve_geo_targets(country_codes)
        if unknown:
            return f"Error: unknown geo tokens {unknown}. Use ISO code (one of {sorted(GEO_TARGET_CONSTANTS.keys())}) or raw numeric geo target constant ID."
        if not resolved:
            return "Error: no geo targets provided."

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        campaign_rn = f"customers/{formatted_id}/campaigns/{campaign_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignCriteria:mutate"

        operations = [
            {
                "create": {
                    "campaign": campaign_rn,
                    "location": {"geoTargetConstant": f"geoTargetConstants/{constant_id}"}
                }
            }
            for _, constant_id in resolved
        ]
        resp = requests.post(url, headers=headers, json={"operations": operations})
        data = resp.json()
        if resp.status_code != 200:
            return f"Error adding geo targets: {json.dumps(data, indent=2)}"
        labels = ", ".join(label for label, _ in resolved)
        return f"Added {len(resolved)} geo target(s) to campaign {campaign_id}: {labels}"
    except Exception as e:
        logger.error(f"Error in add_campaign_geo_targets: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def remove_campaign_geo_targets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    country_codes: str = Field(..., description="Comma-separated ISO codes and/or raw geo target constant IDs to remove. Example: 'NZ,SG,AU' or '1005407'."),
) -> str:
    """
    Remove geographic location targets from a campaign.

    For each token, constructs the campaign_criterion resource name as
    `{campaign_id}~{geoTargetConstantId}` and issues a remove mutation. Tokens may be
    ISO country codes (resolved via the built-in map) or raw numeric geo target
    constant IDs.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        country_codes: Comma-separated ISO codes and/or numeric geo target constant IDs to drop
    """
    try:
        resolved, unknown = _resolve_geo_targets(country_codes)
        if unknown:
            return f"Error: unknown geo tokens {unknown}. Use ISO code (one of {sorted(GEO_TARGET_CONSTANTS.keys())}) or raw numeric geo target constant ID."
        if not resolved:
            return "Error: no geo targets provided."

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignCriteria:mutate"

        operations = [
            {"remove": f"customers/{formatted_id}/campaignCriteria/{campaign_id}~{constant_id}"}
            for _, constant_id in resolved
        ]
        resp = requests.post(url, headers=headers, json={"operations": operations, "partialFailure": True})
        data = resp.json()
        if resp.status_code != 200:
            return f"Error removing geo targets: {json.dumps(data, indent=2)}"
        partial_error = data.get("partialFailureError")
        labels = ", ".join(label for label, _ in resolved)
        if partial_error:
            return f"Partial success removing geo targets from campaign {campaign_id} ({labels}). Some targets did not exist on this campaign:\n{json.dumps(partial_error, indent=2)}"
        return f"Removed {len(resolved)} geo target(s) from campaign {campaign_id}: {labels}"
    except Exception as e:
        logger.error(f"Error in remove_campaign_geo_targets: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def update_ad_group_status(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    ad_group_id: str = Field(..., description="Ad group ID (the numeric part — e.g. 195660215213)"),
    status: str = Field(..., description="New status: ENABLED, PAUSED, or REMOVED"),
) -> str:
    """
    Pause, enable, or remove a single ad group. Useful for concentrating budget on the
    best-performing ad group within a campaign.

    Args:
        customer_id: Google Ads customer ID (10 digits, no dashes)
        ad_group_id: Ad group ID
        status: ENABLED | PAUSED | REMOVED
    """
    status = status.upper()
    if status not in ("ENABLED", "PAUSED", "REMOVED"):
        return "Error: status must be ENABLED, PAUSED, or REMOVED."
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        resource_name = f"customers/{formatted_id}/adGroups/{ad_group_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/adGroups:mutate"
        payload = {
            "operations": [{
                "update": {"resourceName": resource_name, "status": status},
                "updateMask": "status",
            }]
        }
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error updating ad group: {json.dumps(data, indent=2)}"
        return f"Ad group {ad_group_id} status set to {status}."
    except Exception as e:
        logger.error(f"Error in update_ad_group_status: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def add_campaign_language_targets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    language_codes: str = Field(..., description="Comma-separated ISO 639-1 language codes (e.g. 'en' or 'en,de,fr')"),
) -> str:
    """
    Add language targets to a campaign. By default Google targets all languages;
    adding explicit languages restricts delivery to searchers with that browser/account language.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        language_codes: Comma-separated ISO 639-1 codes (en, de, fr, es, ru, uk, etc.)
    """
    try:
        codes = [c.strip().lower() for c in language_codes.split(",") if c.strip()]
        unknown = [c for c in codes if c not in LANGUAGE_CONSTANTS]
        if unknown:
            return f"Error: unknown language codes {unknown}. Supported: {sorted(LANGUAGE_CONSTANTS.keys())}"

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        campaign_rn = f"customers/{formatted_id}/campaigns/{campaign_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignCriteria:mutate"

        operations = [
            {
                "create": {
                    "campaign": campaign_rn,
                    "language": {"languageConstant": f"languageConstants/{LANGUAGE_CONSTANTS[code]}"}
                }
            }
            for code in codes
        ]
        resp = requests.post(url, headers=headers, json={"operations": operations})
        data = resp.json()
        if resp.status_code != 200:
            return f"Error adding language targets: {json.dumps(data, indent=2)}"
        return f"Added {len(codes)} language target(s) to campaign {campaign_id}: {', '.join(codes)}"
    except Exception as e:
        logger.error(f"Error in add_campaign_language_targets: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def add_negative_keywords(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    keywords: str = Field(..., description="Comma-separated negative keywords"),
    match_type: str = Field("BROAD", description="Match type for negatives: BROAD (default), PHRASE, or EXACT"),
) -> str:
    """
    Add campaign-level negative keywords. These block ads from showing on any search
    containing the negative term (with the given match type).

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        keywords: Comma-separated negative keywords (e.g. 'free, jobs, course, youtube')
        match_type: BROAD (default — blocks any search containing all words), PHRASE, or EXACT
    """
    match_type = match_type.upper()
    if match_type not in ("BROAD", "PHRASE", "EXACT"):
        return "Error: match_type must be BROAD, PHRASE, or EXACT."
    try:
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        if not kw_list:
            return "Error: no keywords provided."

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        campaign_rn = f"customers/{formatted_id}/campaigns/{campaign_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignCriteria:mutate"

        operations = [
            {
                "create": {
                    "campaign": campaign_rn,
                    "negative": True,
                    "keyword": {"text": kw, "matchType": match_type},
                }
            }
            for kw in kw_list
        ]
        resp = requests.post(url, headers=headers, json={"operations": operations})
        data = resp.json()
        if resp.status_code != 200:
            return f"Error adding negative keywords: {json.dumps(data, indent=2)}"
        return f"Added {len(kw_list)} negative keyword(s) ({match_type}) to campaign {campaign_id}:\n  • " + "\n  • ".join(kw_list)
    except Exception as e:
        logger.error(f"Error in add_negative_keywords: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def remove_campaign_negative_keywords(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    keywords: str = Field(..., description="Comma-separated negative-keyword texts to remove (exact text match, case-sensitive)."),
    match_type: Optional[str] = Field(None, description="Optional filter — only remove negatives with this match type (BROAD | PHRASE | EXACT). If omitted, removes any match type with the given text."),
) -> str:
    """
    Detach (remove) campaign-level negative keywords by their exact text.

    Looks up active negative-keyword campaign criteria via GAQL, finds those
    whose keyword.text matches one of `keywords` (exact, case-sensitive). If
    `match_type` is given, only criteria with that match type are removed.
    Issues a remove mutation for each matched criterion.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        keywords: Comma-separated negative-keyword texts to remove
        match_type: Optional BROAD/PHRASE/EXACT filter
    """
    try:
        wanted = [k.strip() for k in keywords.split(",") if k.strip()]
        if not wanted:
            return "Error: no keywords provided."
        mt_filter = match_type.upper() if match_type else None
        if mt_filter and mt_filter not in ("BROAD", "PHRASE", "EXACT"):
            return "Error: match_type must be BROAD, PHRASE, or EXACT."

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        # 1. GAQL lookup
        query_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/googleAds:search"
        gaql = (
            "SELECT campaign.id, campaign_criterion.resource_name, "
            "campaign_criterion.keyword.text, campaign_criterion.keyword.match_type "
            "FROM campaign_criterion "
            f"WHERE campaign.id = {campaign_id} "
            "AND campaign_criterion.type = 'KEYWORD' "
            "AND campaign_criterion.negative = TRUE"
        )
        q_resp = requests.post(query_url, headers=headers, json={"query": gaql})
        q_data = q_resp.json()
        if q_resp.status_code != 200:
            return f"Error querying existing negatives: {json.dumps(q_data, indent=2)}"

        # Build set of (text, match_type) → resource_name
        existing = {}
        for row in q_data.get("results", []):
            kw = (row.get("campaignCriterion", {}) or {}).get("keyword") or {}
            text = kw.get("text")
            mt = kw.get("matchType")
            rn = (row.get("campaignCriterion", {}) or {}).get("resourceName")
            if text and rn:
                existing[(text, mt)] = rn

        matched = []
        unmatched = []
        for w in wanted:
            hits = [(k, rn) for k, rn in existing.items()
                    if k[0] == w and (mt_filter is None or k[1] == mt_filter)]
            if hits:
                matched.extend(hits)
            else:
                unmatched.append(w)

        if not matched:
            mt_note = f" with match_type={mt_filter}" if mt_filter else ""
            return f"No matching negative keywords{mt_note} found on campaign {campaign_id}. Unmatched: {unmatched}"

        # 2. Remove
        rm_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignCriteria:mutate"
        ops = [{"remove": rn} for _, rn in matched]
        rm_resp = requests.post(rm_url, headers=headers, json={"operations": ops, "partialFailure": True})
        rm_data = rm_resp.json()
        if rm_resp.status_code != 200:
            return f"Error removing negatives: {json.dumps(rm_data, indent=2)}"

        removed_labels = ", ".join(f"{k[0]} ({k[1]})" for k, _ in matched)
        msg = f"Removed {len(matched)} negative keyword(s) from campaign {campaign_id}: {removed_labels}"
        if unmatched:
            msg += f"\nUnmatched: {unmatched}"
        partial_error = rm_data.get("partialFailureError")
        if partial_error:
            msg += f"\nPartial failure detail: {json.dumps(partial_error, indent=2)}"
        return msg
    except Exception as e:
        logger.error(f"Error in remove_campaign_negative_keywords: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def set_bidding_strategy(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    strategy: str = Field(..., description="MANUAL_CPC | MAXIMIZE_CLICKS | MAXIMIZE_CONVERSIONS | MAXIMIZE_CONVERSION_VALUE"),
    cpc_bid_ceiling_micros: int = Field(0, description="Optional CPC bid ceiling in micros (only for MAXIMIZE_CLICKS). 0 = no ceiling."),
) -> str:
    """
    Change a campaign's bidding strategy.

    MAXIMIZE_CLICKS with cpc_bid_ceiling_micros is the recommended strategy when you want
    Google to optimize for clicks within a CPC cap and have no conversion history yet.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        strategy: One of MANUAL_CPC, MAXIMIZE_CLICKS, MAXIMIZE_CONVERSIONS, MAXIMIZE_CONVERSION_VALUE
        cpc_bid_ceiling_micros: CPC ceiling for MAXIMIZE_CLICKS (e.g. 20000000 = 20 UAH / 20 USD). 0 = unbounded.
    """
    strategy = strategy.upper()
    allowed = {"MANUAL_CPC", "MAXIMIZE_CLICKS", "MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"}
    if strategy not in allowed:
        return f"Error: strategy must be one of {sorted(allowed)}"
    try:
        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)
        resource_name = f"customers/{formatted_id}/campaigns/{campaign_id}"
        url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaigns:mutate"

        update = {"resourceName": resource_name}
        if strategy == "MANUAL_CPC":
            update["manualCpc"] = {}
            update_mask = "manualCpc"
        elif strategy == "MAXIMIZE_CLICKS":
            target_spend = {}
            if cpc_bid_ceiling_micros > 0:
                target_spend["cpcBidCeilingMicros"] = str(cpc_bid_ceiling_micros)
            update["targetSpend"] = target_spend
            update_mask = "targetSpend.cpcBidCeilingMicros" if cpc_bid_ceiling_micros > 0 else "targetSpend"
        elif strategy == "MAXIMIZE_CONVERSIONS":
            update["maximizeConversions"] = {}
            update_mask = "maximizeConversions"
        else:  # MAXIMIZE_CONVERSION_VALUE
            update["maximizeConversionValue"] = {}
            update_mask = "maximizeConversionValue"

        payload = {"operations": [{"update": update, "updateMask": update_mask}]}
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        if resp.status_code != 200:
            return f"Error setting bidding strategy: {json.dumps(data, indent=2)}"
        ceiling = f" (CPC ceiling: {cpc_bid_ceiling_micros} micros)" if strategy == "MAXIMIZE_CLICKS" and cpc_bid_ceiling_micros > 0 else ""
        return f"Campaign {campaign_id} bidding strategy set to {strategy}{ceiling}."
    except Exception as e:
        logger.error(f"Error in set_bidding_strategy: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def add_campaign_callout_assets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    texts: str = Field(..., description="Comma-separated callout texts (each ≤ 25 chars). Example: '244K historical alerts, Backtest in seconds, Free weekly digest, Data, not signals'"),
) -> str:
    """
    Create CalloutAsset objects and attach them to a campaign as CampaignAsset with
    fieldType=CALLOUT. Callouts are short non-clickable benefit phrases (≤ 25 chars)
    shown under the ad. Google recommends 4+ per campaign.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        texts: Comma-separated callout texts. NOTE: commas inside callouts are not supported (the parameter is comma-separated)
    """
    try:
        callout_texts = [t.strip() for t in texts.split(",") if t.strip()]
        if not callout_texts:
            return "Error: no callout texts provided."
        too_long = [t for t in callout_texts if len(t) > 25]
        if too_long:
            return f"Error: callouts must be ≤ 25 chars. Too long: {too_long}"

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        asset_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/assets:mutate"
        asset_ops = [{"create": {"calloutAsset": {"calloutText": t}}} for t in callout_texts]
        asset_resp = requests.post(asset_url, headers=headers, json={"operations": asset_ops})
        asset_data = asset_resp.json()
        if asset_resp.status_code != 200:
            return f"Error creating callout assets: {json.dumps(asset_data, indent=2)}"
        asset_resource_names = [r["resourceName"] for r in asset_data.get("results", [])]

        campaign_rn = f"customers/{formatted_id}/campaigns/{campaign_id}"
        link_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignAssets:mutate"
        link_ops = [
            {"create": {"asset": rn, "campaign": campaign_rn, "fieldType": "CALLOUT"}}
            for rn in asset_resource_names
        ]
        link_resp = requests.post(link_url, headers=headers, json={"operations": link_ops})
        link_data = link_resp.json()
        if link_resp.status_code != 200:
            return f"Created {len(asset_resource_names)} callout assets but failed to link to campaign: {json.dumps(link_data, indent=2)}"
        return f"Added {len(callout_texts)} callout asset(s) to campaign {campaign_id}: {', '.join(repr(t) for t in callout_texts)}"
    except Exception as e:
        logger.error(f"Error in add_campaign_callout_assets: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def remove_campaign_callout_assets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    texts: str = Field(..., description="Comma-separated callout texts to remove (exact match). Example: '244K historical alerts, Old callout text'."),
) -> str:
    """
    Detach (remove) callout assets from a campaign by their exact text.

    Looks up active CampaignAsset(field_type=CALLOUT) entries via GAQL, finds those
    whose calloutText matches one of `texts` (exact, case-sensitive), and removes
    the campaign_asset link. Underlying Asset rows are NOT deleted — they remain
    in the account and can be reused by other campaigns or re-attached later.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        texts: Comma-separated callout texts to remove (each must match an existing
               callout exactly). Unmatched texts are reported but do not fail the op.
    """
    try:
        wanted = [t.strip() for t in texts.split(",") if t.strip()]
        if not wanted:
            return "Error: no callout texts provided."

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        # 1. GAQL lookup of current callout campaign-assets
        query_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/googleAds:search"
        gaql = (
            "SELECT campaign.id, campaign_asset.resource_name, asset.id, "
            "asset.callout_asset.callout_text "
            "FROM campaign_asset "
            f"WHERE campaign.id = {campaign_id} "
            "AND campaign_asset.field_type = 'CALLOUT'"
        )
        q_resp = requests.post(query_url, headers=headers, json={"query": gaql})
        q_data = q_resp.json()
        if q_resp.status_code != 200:
            return f"Error querying existing callouts: {json.dumps(q_data, indent=2)}"

        # Map text → campaign_asset resource name
        existing = {}
        for row in q_data.get("results", []):
            text = (row.get("asset", {}).get("calloutAsset") or {}).get("calloutText")
            rn = (row.get("campaignAsset") or {}).get("resourceName")
            if text and rn:
                existing[text] = rn

        matched = [(t, existing[t]) for t in wanted if t in existing]
        unmatched = [t for t in wanted if t not in existing]
        if not matched:
            return f"No matching callouts found on campaign {campaign_id}. Unmatched: {unmatched}"

        # 2. Remove the campaign_asset links
        link_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignAssets:mutate"
        ops = [{"remove": rn} for _, rn in matched]
        link_resp = requests.post(link_url, headers=headers, json={"operations": ops, "partialFailure": True})
        link_data = link_resp.json()
        if link_resp.status_code != 200:
            return f"Error removing callouts: {json.dumps(link_data, indent=2)}"

        removed_labels = ", ".join(repr(t) for t, _ in matched)
        msg = f"Removed {len(matched)} callout link(s) from campaign {campaign_id}: {removed_labels}"
        if unmatched:
            msg += f"\nUnmatched (no such callout on this campaign): {unmatched}"
        partial_error = link_data.get("partialFailureError")
        if partial_error:
            msg += f"\nPartial failure detail: {json.dumps(partial_error, indent=2)}"
        return msg
    except Exception as e:
        logger.error(f"Error in remove_campaign_callout_assets: {e}")
        return f"Error: {str(e)}"


@mcp.tool()
async def add_campaign_sitelink_assets(
    customer_id: str = Field(..., description="Google Ads customer ID (10 digits, no dashes)"),
    campaign_id: str = Field(..., description="Campaign ID (the numeric part)"),
    sitelinks_json: str = Field(..., description="JSON array of sitelinks. Each item: {link_text (≤25 chars), final_url, description1 (≤35 chars, optional), description2 (≤35 chars, optional)}. Note: if you supply description1 you MUST also supply description2. Example: '[{\"link_text\":\"Pricing\",\"final_url\":\"https://filtrix.net/pricing\",\"description1\":\"Free + paid tiers\",\"description2\":\"Cancel anytime\"}]'"),
) -> str:
    """
    Create SitelinkAsset objects and attach them to a campaign as CampaignAsset with
    fieldType=SITELINK. Sitelinks are clickable links shown under the ad. Google
    recommends 4-6 per campaign for max display eligibility.

    Args:
        customer_id: Google Ads customer ID
        campaign_id: Campaign ID
        sitelinks_json: JSON array. Each entry needs link_text + final_url; optional description1+description2 (must come as a pair).
    """
    try:
        try:
            sitelinks = json.loads(sitelinks_json)
        except json.JSONDecodeError as e:
            return f"Error: sitelinks_json is not valid JSON: {e}"
        if not isinstance(sitelinks, list) or not sitelinks:
            return "Error: sitelinks_json must be a non-empty JSON array."

        errors: list[str] = []
        for i, s in enumerate(sitelinks):
            if not isinstance(s, dict):
                errors.append(f"item[{i}] must be an object")
                continue
            if not s.get("link_text") or not s.get("final_url"):
                errors.append(f"item[{i}] needs link_text and final_url")
                continue
            if len(s["link_text"]) > 25:
                errors.append(f"item[{i}] link_text > 25 chars: {s['link_text']!r}")
            d1, d2 = s.get("description1"), s.get("description2")
            if (d1 and not d2) or (d2 and not d1):
                errors.append(f"item[{i}] description1 and description2 must be both set or both omitted")
            if d1 and len(d1) > 35:
                errors.append(f"item[{i}] description1 > 35 chars")
            if d2 and len(d2) > 35:
                errors.append(f"item[{i}] description2 > 35 chars")
        if errors:
            return "Error(s) in sitelinks_json:\n  - " + "\n  - ".join(errors)

        creds = get_credentials()
        headers = get_headers(creds)
        formatted_id = format_customer_id(customer_id)

        asset_ops = []
        for s in sitelinks:
            sitelink_asset = {"linkText": s["link_text"]}
            if s.get("description1"):
                sitelink_asset["description1"] = s["description1"]
                sitelink_asset["description2"] = s["description2"]
            asset_ops.append({"create": {
                "finalUrls": [s["final_url"]],
                "sitelinkAsset": sitelink_asset,
            }})

        asset_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/assets:mutate"
        asset_resp = requests.post(asset_url, headers=headers, json={"operations": asset_ops})
        asset_data = asset_resp.json()
        if asset_resp.status_code != 200:
            return f"Error creating sitelink assets: {json.dumps(asset_data, indent=2)}"
        asset_resource_names = [r["resourceName"] for r in asset_data.get("results", [])]

        campaign_rn = f"customers/{formatted_id}/campaigns/{campaign_id}"
        link_url = f"https://googleads.googleapis.com/{API_VERSION}/customers/{formatted_id}/campaignAssets:mutate"
        link_ops = [
            {"create": {"asset": rn, "campaign": campaign_rn, "fieldType": "SITELINK"}}
            for rn in asset_resource_names
        ]
        link_resp = requests.post(link_url, headers=headers, json={"operations": link_ops})
        link_data = link_resp.json()
        if link_resp.status_code != 200:
            return f"Created {len(asset_resource_names)} sitelink assets but failed to link to campaign: {json.dumps(link_data, indent=2)}"
        labels = ", ".join(s["link_text"] for s in sitelinks)
        return f"Added {len(sitelinks)} sitelink asset(s) to campaign {campaign_id}: {labels}"
    except Exception as e:
        logger.error(f"Error in add_campaign_sitelink_assets: {e}")
        return f"Error: {str(e)}"


if __name__ == "__main__":
    # Start the MCP server on stdio transport
    mcp.run(transport="stdio")
