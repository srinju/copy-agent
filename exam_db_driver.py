from pymongo import MongoClient
from typing import List, Optional
from dataclasses import dataclass
from bson import ObjectId
import logging
import os

logger = logging.getLogger("exam_db")

@dataclass
class ExamQuestion:
    text: str

@dataclass
class Exam:
    exam_id: str
    name: str
    questions: List[ExamQuestion]
    duration: int
    difficulty: str

class ExamDBDriver:
    def __init__(self, mongo_uri: str = None, db_name: str = "coral-ai"):
        self.client = MongoClient(mongo_uri or os.getenv("MONGO_URI"))
        self.db = self.client[db_name]
        self.exams_collection = self.db["exams"]
        logger.info("Connected to MongoDB")

    def get_exam_by_id(self, exam_id: str) -> Optional[Exam]:
        try:
            logger.info(f"Attempting to fetch exam with ID: {exam_id}")
            
            if not ObjectId.is_valid(exam_id):
                logger.error(f"Invalid exam ID format: {exam_id}")
                return None

            exam_data = self.exams_collection.find_one({"_id": ObjectId(exam_id)})
            
            if not exam_data:
                logger.error(f"No exam found for ID: {exam_id}")
                return None
            
            logger.info(f"Found exam: {exam_data.get('name', 'Unnamed')} with {len(exam_data.get('questions', []))} questions")
                
            return Exam(
                exam_id=str(exam_data["_id"]),
                name=exam_data.get("name", "Unnamed Exam"),
                questions=[ExamQuestion(text=q.get("text", "")) for q in exam_data.get("questions", [])],
                duration=exam_data.get("duration", 0),
                difficulty=exam_data.get("difficulty", "Medium")
            )
        
        except Exception as e:
            logger.error(f"MongoDB Error: {str(e)}", exc_info=True)
            return None
