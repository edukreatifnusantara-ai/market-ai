"""Registrasi semua agen."""
from .researcher import Researcher
from .strategist import Strategist
from .producer import Producer
from .icp import ICPAgent
from .content import ContentAgent
from .emailer import EmailAgent
from .whatsapp import WhatsAppAgent
from .followup import FollowUpAgent
from .sentinel import Sentinel

ALL_AGENTS = [Researcher, Strategist, Producer, ICPAgent, ContentAgent,
              EmailAgent, WhatsAppAgent, FollowUpAgent, Sentinel]
