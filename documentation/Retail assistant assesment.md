Your role is that of a Principal AI Solutions Architect and Tech Lead, preparing to execute this test assignment (designing a production-grade HLD and developing a functional CLI prototype).  
Your goal - The prototype must implement four requirements: Safety \& PII Masking, High-Stakes Oversight (destructive ops), Resilience \& Graceful Error Handling, and Observability. The other requirements (Hybrid Intelligence / Golden Bucket, Continuous Improvement, Quality Assurance, Persona Management) are design-only, so cover them in the HLD and technical explanation. The HLD should also describe how you would handle every requirement in a production-grade system. Adding assumptions and disclaimers where something is unclear is fine.  
All of these must follow DDD domain driven development, SRP, Hexagonal architecture, KISS, YAGNI, DRY)  
Your task is to do GAP requirements-solution analysis

AI Technical Assignment - Retail Company:

The Challenge: Design and Build a Data Analysis Chat Assistant

You are building data analysis agent for a Retail Company's non-technical executive teams (OpsFleet is the client for this project and the Team Lead + Harry are the Project managers). These executives need to ask complex questions about sales, and performance (e.g., "Why are the users for sate X underspending? And how does it compare to users in state Y?", " Why did our churn rate spike last month? ", "create a report for Q1 including insights and actions items for Q2")  
The agent will have access to:

\* The Database: A read-only connection to a SQL database containing raw transaction logs.  
\* The "Golden Knowledge" Bucket: A data lake containing historical "Trios" (Question → SQL Query → Analyst Report) created by human experts (Theoretical).

The Task: Design a production-ready full High-Level Design (HLD), accompanied by a detailed technical explanation, and build a data analysis chat-agent prototype that allows executives to ask question about this data naturally and discuss about it. The system should be easily extendable for new capabilities (generating graphs, sending reports via mail, searching the web for trends, etc).  
Note: We expect to see what services you aim to use, what the communication is between the components, how and where data is stored and handled. The "detailed technical explanation" needs to be detailed enough for us to understand how this system will function in production.  
Feel free to ask about any assumptions you need to make, or follow ups on requirements, we (OpsFleet team) are the client for this project.

Requirements:

1\. Hybrid Intelligence: The agent cannot rely on BQ data alone. It must use the "Golden Bucket" to understand how analysts previously interpreted questions and apply similar logic to new queries. Explain how you will update the golden bucket over time and how you will provide relevant data at user query time.  
2. Safety \& PII Masking: The agent is only allowed to answer analysis questions and needs to be safeguarded against malicious users. The raw transaction logs contain sensitive data. The agent is strictly forbidden from displaying these PIIs in the final output. Each user should only be able to analyze data on products related to him. Per the assignment, each manager is responsible for one or more brands and can only analyze those brands, while the CEO sees everything. Brand is the only access dimension. Customer and order data should only be visible through purchases of in-scope products. The manager-to-brand mapping is not in the dataset, so assume whatever structure you think is appropriate, such as a config or access table, and document it in your design.  
3. High-Stakes Oversight (Destructive Ops): While the DB is read-only, the agent manages a "Saved Reports" library. The agent must support inputs like "Delete all reports mentioning Client X" / "Delete all the reports we made in this conversation" This is a destructive action and requires a strict confirmation flow before execution, without breaking UX - the users are allowed to delete their own reports.  
4. Continuous Improvement (The Learning Loop):  
1. User Level: The agent should remember that "User A" prefers tables while "User B" prefers bullet points, as well as learn user preferences over time for example: how deep they like the analysis, do they prefer charts or text, etc.  
2. System Level: The agent should be able to learn from past interactions and improve itself.  
5. Resilience \& Graceful Error Handling: The system must detect syntax errors or empty returns and attempt to self-correct before giving up, without crashing the user interface and without inflating costs. The system should be resilient to API/3rd party services failures/downtime  
6. Quality Assurance: How do you evaluate the agent before deployment? How do you verify that the generated reports answer users intent correctly? How do you evaluate UX?  
7. Observability: We need to know when the agent is failing and why. Define the metrics you would track at the agent level and how you would support deep dive analysis/debugging (understanding what the message correspondence is and what went wrong).  
8. Agility (Persona Management): The CEO wants to change the "tone" of the reports weekly. The system must allow non-developers to update the agent's instructions without redeployment.

Deliverables:

1\. Architecture Diagram (Mermaid etc): Highlighting the building blocks, services, compute and flow of the system based on the requirements above. In case you are planning to use a infrastructure/framework/service/data store for one of the building blocks, specify which one and include an explanation.  
2. Detailed technical explanation covering:  
1. Reasoning for the chosen Cloud services / LLM models/frameworks used.  
2. Data flow between components (if needs to elaborate on HLD).  
3. Error handling and fallback strategies.  
4. Setup Instructions and example run  
5. Make sure to include a detailed explanation of how you handle/solve each of the requirements  
3.  Working Code/Prototype: Build a chat agent that allows users to ask data related questions naturally, discuss about it and create a report with action items when asked to. The prototype needs to support all of these following requirements (defined above, rest of the requirements are for design only):  
1. Safety \& PII Masking  
2. High-Stakes Oversight  
3. Resilience \& Graceful Error Handling  
4. Observability  
4. CLI-based interface for chat interactions.  
5. Your solution must be runnable on another machine (Docker is not a must, just proper setup instructions).  
6. Use a framework of your choice, explain why you choose it and your level of experience with it.

Our assessment will focus mainly on system design, the technical explanation, and an elegant Prototype that fits the Prototype requirements

Dataset Specification:

\* Dataset: bigquery-public-data.thelook\_ecommerce  
\* Required Tables:  
\* orders - Customer order information  
\* order\_items - Individual items within orders  
\* products - Product catalog and details  
\* users - Customer demographics and information

Expected Agent Capabilities:

Your prototype chat agent should be able to perform data analysis and generate insightful reports such as:

\* Customer behavior (e.g., top customers, total spend)  
\* Product performance (e.g compare performance of product X and Y, and why do they perform differently)  
\* Time-based metrics (e.g., monthly revenue, up-to-date revenue by product)  
\* Answer questions about the general structure of the database (what data is available, what can we do with it)  
\* Multi step queries (as described above)

1\. Use BigQuery integration to query and analyze the specified tables. Your agent should be able to construct and execute SQL queries dynamically based on the analysis requirements.  
2. You should preferably use one of the newer Google Gemini models. You can get a free API key from Google AI Studio. Please be mindful of the rate limits. - If you use another provider please make sure you support this as this is what we will use for testing.

BigQuery Setup

1\. Set up BigQuery access by following the BigQuery Client Libraries documentation if you don't already have BigQuery access configured.  
2. Free Tier: Google Cloud provides 1TB of free BigQuery compute per month, which is more than sufficient for this challenge.  
3. Authentication: Ensure your environment is authenticated with Google Cloud to access the public datasets.

Disclaimers:  
There is no single company-wide definition of metrics such as churn or net revenue, and each brand manager can have their own. The tables are raw, and another team handles the data engineering. Define a lightweight glossary or config yourself with sensible defaults (e.g. churn as no purchase in 90 days, revenue excluding cancelled and returned items). State the definition used in each answer and let users override it.  
The AI agent should construct and execute SQL dynamically on the raw tables, not only use pre-governed metrics. Checking the generated SQL, enforcing the brand/product access, and hiding small demographic groups (minimum group size 5 by default, configurable) in code is enough for the prototype. Demographic groups below the minimum size (5 by default, configurable) should be hidden. Please document your approach in the design doc.  
PII: first and last name, email, street address, postal code and exact coordinates must never be shown in the output, and PII must never reach the LLM, so mask it before data is sent to the model. City, state, country, age, gender and traffic source may be shown only in aggregates. Hide demographic groups below a minimum size (5 by default, configurable). Referring to customers by the internal user\_id or a stable pseudonymous ID is fine.  
Compliance: there are no data compliance requirements for this assignment. PII still must not reach the LLM or appear in the output.  
Cloud or serverless or selfhosted: you can choose whichever cloud you prefer, or design a cloud-agnostic setup. Specify which services you use for each building block and explain why. We mainly work with AWS, so it's preferred, while model calls still go to Gemini (free tier) as per the brief.  
Users and concurrency: tens to a few hundred executives, with only a few active at the same time. For production sizing assume about 100 active managers per day, about 10 questions per manager per day, about 1 saved report per manager per day, and daily data updates of about 1M new rows. Simple questions should take 10-30 seconds, and long reports 1-2 minutes.  
Limits: there is a budget ceiling per question, which should be easily configurable. You can assume $1 per question. This is design only and does not need to be implemented in the prototype. Keep the number of LLM calls per query low (roughly 2-5). Google provides 1 TB of free BigQuery processing per month, which is more than enough for this task.  
user\_id with demographics: the first option. Referring to a customer by user\_id or a stable pseudonymous ID is fine, but demographic values (age, gender, city, state, country, traffic source) may be shown only in aggregates, with groups of at least 5 by default (configurable). A row like "customer 123, female, Berlin" ties an attribute to one person, so it should be blocked, or answered at aggregate level instead. Please document the approach you choose in the design doc.  
For the prototype, local tracing/logging is fine (e.g. SQLite or JSON logs instead of Prometheus/Grafana/Langfuse), and you can use the tooling you're most comfortable with. Please describe the intended production observability tooling in your HLD, and explain your choice and the trade-offs. We need to know when the agent is failing and why, so define agent-level metrics and support deep-dive debugging (the full message correspondence and what went wrong). On whether LangSmith specifically is a good fit, I'm not the right one to answer this. LangSmith is ok for observability, no specific preferred tool, what ever you have most experience with and you feel is a solid engineering choice for solving the task  
User identity: the manager-to-brand mapping is not in the dataset, so assume whatever structure you think is appropriate, such as a config or access table, and document it. In production the frontend would send a JWT with the user's scopes (e.g. accessible brands), and the agent should enforce access based on those scopes.  
Multi-brand orders: a manager's metrics (revenue, order counts, customer spend, churn, rankings) are computed only from purchases of their own authorized brands.  
Reports: save a report only when the user explicitly asks for one. A saved report contains the question, the SQL and the answer, and only its author can see it. Destructive actions such as 'Delete all the reports we made in this conversation' require a strict confirmation flow, such as showing the exact list of reports before a hard delete.  
There are no internal standards for HLD, schemas, config examples, etc., something you think will be a good deliverable to a real client. Schemas and configs are a bit too much, but the eval set example is fine. Try to have it on the agent flow + services you use mainly

bq\_client.py:  
"import logging  
from typing import Optional, List, Dict, Any  
import pandas as pd  
from google.cloud import bigquery  
​  
​  
class BigQueryRunner:  
"""A lean BigQuery client for executing SQL queries and returning DataFrame results."""

&#x20;   def \\\_\\\_init\\\_\\\_(self, project\\\_id: Optional\\\[str\\] \\= None, dataset\\\_id: Optional\\\[str\\] \\= "bigquery-public-data.thelook\\\_ecommerce") \\-\\> None:  
        """Initialize BigQuery client.  
          
        Args:  
            project\\\_id: Google Cloud project ID. If None, uses default credentials.  
            dataset\\\_id: BigQuery dataset ID. If None, uses default dataset.  
        """  
        logging.info("Initializing BigQuery client")  
        try:  
            self.client \\= bigquery.Client(project=project\\\_id)  
            self.dataset\\\_id \\= dataset\\\_id  
            logging.info(f"BigQuery client initialized for dataset: {self.dataset\\\_id}")  
        except Exception as e:  
            logging.error(f"Failed to initialize BigQuery client: {str(e)}")  
            raise  
      
    def execute\\\_query(self, sql\\\_query: str) \\-\\> pd.DataFrame:  
        """Execute a SQL query and return results as a DataFrame.  
          
        Args:  
            sql\\\_query: The SQL query to execute.  
              
        Returns:  
            DataFrame containing the query results.  
              
        Raises:  
            Exception: If query execution fails.  
        """  
        try:  
            logging.info(f"Executing BigQuery query")  
            query\\\_job \\= self.client.query(sql\\\_query)  
            df \\= query\\\_job.result().to\\\_dataframe()  
            logging.info(f"Query completed successfully, returned {len(df)} rows")  
            return df  
        except Exception as e:  
            logging.error(f"BigQuery execution failed: {str(e)}")  
            raise   

​  
def get\_table\_schema(self, table\_name: str) -> List\[Dict\[str, Any]]:  
"""Get schema information for a specific table.

&#x20;       Args:  
            table\\\_name: Name of the table (orders, order\\\_items, products, users).  
              
        Returns:  
            List of dictionaries containing column information.  
        """  
        try:  
            table\\\_ref \\= f"{self.dataset\\\_id}.{table\\\_name}"  
            table \\= self.client.get\\\_table(table\\\_ref)  
            schema\\\_info \\= \\\[\\]  
            for field in table.schema:  
                schema\\\_info.append({  
                    "name": field.name,  
                    "type": field.field\\\_type,  
                    "mode": field.mode,  
                    "description": field.description or ""  
                })  
            logging.info(f"Retrieved schema for table {table\\\_name}")  
            return schema\\\_info  
        except Exception as e:  
            logging.error(f"Failed to get schema for table {table\\\_name}: {str(e)}")  
            raise"


Dataset structure:  
[users.bq](http://users.bq).table links:

|Source|Relationship|Destination|Type|
|-|-|-|-|
|events|events.user\_id = users.id|users|JOIN|
|events|id|users|JOIN|

[users.bq](http://users.bq).table schema:

|Name|Type|Mode|
|-|-|-|
|id|INTEGER|NULLABLE|
|first\_name|STRING|NULLABLE|
|last\_name|STRING|NULLABLE|
|email|STRING|NULLABLE|
|age|INTEGER|NULLABLE|
|gender|STRING|NULLABLE|
|state|STRING|NULLABLE|
|street\_address|STRING|NULLABLE|
|postal\_code|STRING|NULLABLE|
|city|STRING|NULLABLE|
|country|STRING|NULLABLE|
|latitude|FLOAT|NULLABLE|
|longitude|FLOAT|NULLABLE|
|traffic\_source|STRING|NULLABLE|
|created\_at|TIMESTAMP|NULLABLE|
|user\_geom|GEOGRAPHY|NULLABLE|

[products.bq](http://products.bq).table links:

|Source|Relationship|Destination|Type|
|-|-|-|-|
|products|id|users|JOIN|
|orders|orders.order\_id = products.id|products|JOIN|
|order\_items|order\_items.product\_id = products.id|products|JOIN|
|order\_items|id|products|JOIN|
|order\_items|order\_items.order\_id = products.id|products|JOIN|
|order\_items|order\_items.user\_id = products.id|products|JOIN|

[products.bq](http://products.bq).table schema:

|Name|Type|Mode|
|-|-|-|
|id|INTEGER|NULLABLE|
|cost|FLOAT|NULLABLE|
|category|STRING|NULLABLE|
|name|STRING|NULLABLE|
|brand|STRING|NULLABLE|
|retail\_price|FLOAT|NULLABLE|
|department|STRING|NULLABLE|
|sku|STRING|NULLABLE|
|distribution\_center\_id|INTEGER|NULLABLE|

order[s.bq](http://products.bq).table links:

|Source|Relationship|Destination|Type|
|-|-|-|-|
|events|created\_at|orders|JOIN|
|events|created\_at user\_id|orders|JOIN|

order[s.bq](http://products.bq).table schema:

|Name|Type|Mode|
|-|-|-|
|order\_id|INTEGER|NULLABLE|
|user\_id|INTEGER|NULLABLE|
|status|STRING|NULLABLE|
|gender|STRING|NULLABLE|
|created\_at|TIMESTAMP|NULLABLE|
|returned\_at|TIMESTAMP|NULLABLE|
|shipped\_at|TIMESTAMP|NULLABLE|
|delivered\_at|TIMESTAMP|NULLABLE|
|num\_of\_item|INTEGER|NULLABLE|

order\_items[.bq](http://products.bq).table schema:

|Name|Type|Mode|
|-|-|-|
|id|INTEGER|NULLABLE|
|order\_id|INTEGER|NULLABLE|
|user\_id|INTEGER|NULLABLE|
|product\_id|INTEGER|NULLABLE|
|inventory\_item\_id|INTEGER|NULLABLE|
|status|STRING|NULLABLE|
|created\_at|TIMESTAMP|NULLABLE|
|shipped\_at|TIMESTAMP|NULLABLE|
|delivered\_at|TIMESTAMP|NULLABLE|
|returned\_at|TIMESTAMP|NULLABLE|
|sale\_price|FLOAT|NULLABLE|



