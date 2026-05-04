# AI Automation Challenge: Estimating Agent

## Overview
Welcome to the Eaton Constrcution Services Technical Assessment. The goal of this challenge is to build an intelligent agent capable of automating the initial phases of construction estimating. You will be processing complex project data to extract actionable insights for our estimating team.

## The Challenge
You are provided with a set of project manuals, architectural drawings, and existing Bids. Your task is to develop an AI-driven pipeline that performs two core functions:

### Phase 1 Scope Extraction & Trade Assignment
- Analyze Project Data: Parse through the provided project manuals and drawings to understand the overall project context.

- Define Scope of Work (SOW): Identify the specific tasks, materials, and requirements for the project.

- Assign Trades (Trade_List): Categorize the identified scope into relevant construction trades (e.g., Electrical, Plumbing, HVAC, Drywall).

### Phase 2: Bid Analysis & Line-Item Verification
- Scope Checks: For each trade identified in Phase 1, analyze the associated Bid documents to ensure they align with the project requirements.

- Price Breakdown: Extract a detailed price breakdown from the Bids.

- Inclusions & Exclusions: Identify and list what is specifically included or excluded in the bid based on your generated scope checks.

## Repository Structure
- /trade_list: All the trade list based on CSI Master Format.[OneDrive Documents](https://eatonconstructionserv-my.sharepoint.com/:f:/g/personal/shubham_eatonprojects_com/IgCaJ5_vydZJSZylb_J5pSj4AXnXy7CZvzwDzDicbox3n24?e=hByBfD)

- /manuals: Project specifications and guidelines.[OneDrive Documents](https://eatonconstructionserv-my.sharepoint.com/:f:/g/personal/shubham_eatonprojects_com/IgCaJ5_vydZJSZylb_J5pSj4AXnXy7CZvzwDzDicbox3n24?e=hByBfD)

- /drawings: Architectural and structural blueprints.[OneDrive Documents](https://eatonconstructionserv-my.sharepoint.com/:f:/g/personal/shubham_eatonprojects_com/IgCaJ5_vydZJSZylb_J5pSj4AXnXy7CZvzwDzDicbox3n24?e=hByBfD)

- /bids: Sample subcontractor bid documents.[OneDrive Documents](https://eatonconstructionserv-my.sharepoint.com/:f:/g/personal/shubham_eatonprojects_com/IgCaJ5_vydZJSZylb_J5pSj4AXnXy7CZvzwDzDicbox3n24?e=hByBfD)

## Submission Instructions
1. Fork this repository to your personal GitHub account.

2. Create a new branch named solution-yourname.

3. Implement your agent (Python/Node.js preferred).

4. Provide a SOLUTION.md file explaining your architecture, choice of LLM/framework (e.g., LangChain, RAG), and how to run your code.

5. Make a demonstration video of not more than 10 minutes to give walk-through of your application.

6. Submit the assignment by creating a Pull Request back to this main repository and acknowledge to shubham@eatonprojects.com.

## Requirements & Evaluation
- Accuracy: How precisely does the agent identify the scope within dense manuals?

- Structure: Is the output (Scope/Trades/Bids) formatted in a way that an estimator can use?

- Extraction: Can the agent correctly identify missing items (exclusions) in the bids?

- Evaluation Pipeline: Focused on RAGAS for evaluation of the performance of the agent.

- UI: Implement with Fast API to setup the backend python application with client side rendering (React or Nextjs).

##  Deadline
All submissions must be completed by May 7, 2026, at 06:00 PM EST.

Note: Late submissions will not be considered. We recommend performing a test push early to ensure your environment is set up correctly.

## License
This project is licensed under the [MIT License](https://github.com/autobid-eatonprojects/AI_Assessment_ECS/blob/03f6596b02577078e845f354494c4bdf54cd1a44/LICENSE)

### Disclaimer
- Assessment Use Only: The materials provided in this repository (including manuals, drawings, and bids) are for the sole purpose of the Eaton Constrcution Services technical assessment.

- No Warranty: The software and documentation are provided "as is," without warranty of any kind, express or implied.

- Proprietary Data: While this challenge is public, the specific business logic and project data remain the intellectual property of the organization.

Liability: In no event shall the authors or copyright holders be liable for any claim, damages, or other liability arising from the use of this assessment material.