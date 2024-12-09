import asyncio
import logging
from typing import Any, Dict
from asyncio import create_task
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from uuid6 import uuid7

from app.celery.tasks.parsing_tasks import process_parsing
from app.modules.github.github_service import GithubService
from app.modules.parsing.graph_construction.parsing_helper import ParseHelper
from app.modules.parsing.graph_construction.parsing_schema import ParsingRequest
from app.modules.parsing.graph_construction.parsing_service import ParsingService
from app.modules.parsing.graph_construction.parsing_validator import (
    validate_parsing_input,
)
from app.modules.projects.projects_schema import ProjectStatusEnum
from app.modules.projects.projects_service import ProjectService
from app.modules.utils.posthog_helper import PostHogClient
from app.modules.utils.email_helper import EmailHelper
logger = logging.getLogger(__name__)


class ParsingController:
    @staticmethod
    @validate_parsing_input
    async def parse_directory(
        repo_details: ParsingRequest, db: AsyncSession, user: Dict[str, Any]
    ):
        user_id = user["user_id"]
        user_email = user["email"]
        project_manager = ProjectService(db)
        parse_helper = ParseHelper(db)
        parsing_service = ParsingService(db, user_id)
        repo_name = repo_details.repo_name or repo_details.repo_path.split("/")[-1]

        demo_repos = [
            "Portkey-AI/gateway",
            "crewAIInc/crewAI",
            "mem0ai/mem0",
            "AgentOps-AI/agentops",
            "calcom/cal.com",
            "SigNoz/signoz",
        ]

        try:
            project = await project_manager.get_project_from_db(
                repo_name, repo_details.branch_name, user_id
            )
            duplicate_project = True
            demo_project = False
            if project and project.repo_name in demo_repos:
                if project.status == ProjectStatusEnum.READY.value:
                    duplicate_project = False
                    demo_project = True

            if project:
                project_id = project.id
                project_status = project.status
                response = {"project_id": project_id, "status": project_status}

                # Check commit status
                is_latest = (
                    await parse_helper.check_commit_status(project_id)
                    if not demo_project
                    else True
                )

                if not is_latest or project_status != ProjectStatusEnum.READY.value:
                    cleanup_graph = True

                    logger.info(
                        f"Submitting parsing task for existing project {project_id}"
                    )
                    process_parsing.delay(
                        repo_details.model_dump(),
                        user_id,
                        user_email,
                        project_id,
                        cleanup_graph,
                    )

                    response["status"] = ProjectStatusEnum.SUBMITTED.value
                    PostHogClient().send_event(
                        user_id,
                        "parsed_repo_event",
                        {
                            "repo_name": repo_details.repo_name,
                            "branch": repo_details.branch_name,
                            "project_id": project_id,
                        },
                    )
                return response
            else:
                if repo_details.repo_name in demo_repos:
                    existing_project = await project_manager.get_global_project_from_db(
                        repo_name, repo_details.branch_name
                    )

                    new_project_id = str(uuid7())

                    if existing_project:
                        if duplicate_project:
                            await project_manager.duplicate_project(
                                repo_name,
                                repo_details.branch_name,
                                user_id,
                                new_project_id,
                                existing_project.properties,
                                existing_project.commit_id,
                            )
                            await project_manager.update_project_status(
                                new_project_id, ProjectStatusEnum.SUBMITTED
                            )

                            old_repo_id = await project_manager.get_demo_repo_id(
                                repo_name
                            )

                            asyncio.create_task(
                                GithubService(db).get_project_structure_async(
                                    new_project_id
                                )
                            )
                            # Duplicate the graph under the new repo ID
                            await parsing_service.duplicate_graph(
                                old_repo_id, new_project_id
                            )

                            # Update the project status to READY after copying
                            await project_manager.update_project_status(
                                new_project_id, ProjectStatusEnum.READY
                            )
                            create_task(EmailHelper().send_email(user_email, repo_name, repo_details.branch_name))


                        return {
                            "project_id": new_project_id,
                            "status": ProjectStatusEnum.READY.value,
                        }
                    else:
                        return await ParsingController.handle_new_project(
                            repo_details,
                            user_id,
                            user_email,
                            new_project_id,
                            project_manager,
                            db,
                        )

                else:
                    new_project_id = str(uuid7())
                    return await ParsingController.handle_new_project(
                        repo_details,
                        user_id,
                        user_email,
                        new_project_id,
                        project_manager,
                        db,
                    )

        except Exception as e:
            logger.error(f"Error in parse_directory: {e}")
            raise HTTPException(status_code=500, detail="Internal server error")

    @staticmethod
    async def handle_new_project(
        repo_details: ParsingRequest,
        user_id: str,
        user_email: str,
        new_project_id: str,
        project_manager: ProjectService,
        db: AsyncSession,
    ):
        response = {
            "project_id": new_project_id,
            "status": ProjectStatusEnum.SUBMITTED.value,
        }

        logger.info(f"Submitting parsing task for new project {new_project_id}")
        repo_name = repo_details.repo_name or repo_details.repo_path.split("/")[-1]
        await project_manager.register_project(
            repo_name, repo_details.branch_name, user_id, new_project_id
        )
        asyncio.create_task(
            GithubService(db).get_project_structure_async(new_project_id)
        )
        process_parsing.delay(
            repo_details.model_dump(),
            user_id,
            user_email,
            new_project_id,
            False,
        )
        PostHogClient().send_event(
            user_id,
            "repo_parsed_event",
            {
                "repo_name": repo_details.repo_name,
                "branch": repo_details.branch_name,
                "project_id": new_project_id,
            },
        )
        return response

    @staticmethod
    async def fetch_parsing_status(
        project_id: str, db: AsyncSession, user: Dict[str, Any]
    ):
        try:
            project_service = ProjectService(db)
            parse_helper = ParseHelper(db)
            project = await project_service.get_project_from_db_by_id_and_user_id(
                project_id, user["user_id"]
            )
            if project:
                is_latest = await parse_helper.check_commit_status(project_id)
                return {"status": project["status"], "latest": is_latest}
            else:
                raise HTTPException(status_code=404, detail="Project not found")
        except Exception as e:
            logger.error(f"Error in fetch_parsing_status: {str(e)}")
            raise HTTPException(status_code=500, detail="Internal server error")
