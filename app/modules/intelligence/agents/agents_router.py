from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List

from app.core.database import get_db
from app.modules.auth.auth_service import AuthService
from app.modules.intelligence.agents.agents_controller import AgentsController
from app.modules.intelligence.agents.agents_schema import AgentInfo

router = APIRouter()

class AgentsAPI:
    def __init__(self, db: Session = Depends(get_db)):
        self.controller = AgentsController(db)

    @staticmethod
    @router.get("/list-available-agents/", response_model=List[AgentInfo])
    async def list_available_agents(
        db: Session = Depends(get_db),
        user=Depends(AuthService.check_auth),
        list_system_agents: bool = Query(default=True, description="Include system agents in the response")
    ):
        controller = AgentsController(db)
        return await controller.list_available_agents(user, list_system_agents)
