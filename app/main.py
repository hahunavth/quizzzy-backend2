"""Main python file for flask application.

This module handles all API requests.

@Author: Karthick T. Sharma
"""

# pylint: disable=no-name-in-module
import os
import uuid
from pathlib import Path
from fastapi import FastAPI, BackgroundTasks, HTTPException, Depends
from pydantic import BaseModel, EmailStr, constr
from fastapi.responses import JSONResponse, FileResponse
import re
import xml.etree.ElementTree as ET
from auth import JWTBearer

from src.inferencehandler import inference_handler
from src.ansgenerator.false_answer_generator import FalseAnswerGenerator
from src.model.abstractive_summarizer import AbstractiveSummarizer
from src.model.question_generator import QuestionGenerator
from src.model.keyword_extractor import KeywordExtractor
from src.service.firebase_service import FirebaseService
from deep_translator import GoogleTranslator
from datetime import datetime, timedelta


# initialize fireabse client
fs = FirebaseService()

# initialize question and ans models
summarizer = AbstractiveSummarizer()
question_gen = QuestionGenerator()
false_ans_gen = FalseAnswerGenerator()
keyword_extractor = KeywordExtractor()

# FastAPI setup
app = FastAPI()

# Định nghĩa một biến dùng cho xác thực
auth_scheme = JWTBearer()


def generate_que_n_ans(context):
    """Generate question from given context.

    Args:
        context (str): input corpus needed to generate question.

    Returns:
        tuple[list[str], list[str], list[list[str]]]: tuple of lists of all
        generated questions n' answers.
    """
    summary, splitted_text = inference_handler.get_all_summary(
        model=summarizer, context=context)
    filtered_kws = keyword_extractor.get_keywords(
        original_list=splitted_text, summarized_list=summary)

    crct_ans, all_answers = false_ans_gen.get_output(filtered_kws=filtered_kws)
    questions = inference_handler.get_all_questions(
        model=question_gen, context=summary, answer=crct_ans)

    return questions, crct_ans, all_answers


def process_request(request):
    """Process user request and return generated questions to their firestore database.

    Args:
        request (ModelInput): request from flutter.
    """
    request.context = vietnamese_to_english(request.context)
    request.name = vietnamese_to_english(request.name)

    fs.update_generated_status(request, True)
    questions, crct_ans, all_ans = generate_que_n_ans(request.context)
    fs.update_generated_status(request, False)
    # fs.send_results_to_fs(request, questions, crct_ans, all_ans, request.context)
    # Sửa ở đây: Trả về kết quả từ hàm send_results_to_fs
    results = fs.send_results_to_fs(request, questions, crct_ans, all_ans, request.context)
    return results


# body classes for req n' res
# pylint: disable=too-few-public-methods
class ModelInput(BaseModel):
    """General request model structure for flutter incoming req."""
    context: str
    uid: str
    name: str

class ModelExportInput(BaseModel):
    """Request model for exporting questions."""
    uid: str
    name: str

#son
class Rating(BaseModel):
    """Rating questions."""
    uid: str
    rate: int
class ModelRatingInput(BaseModel):
    """Rating questions."""
    uid: str
    name: str
    question_id: str
    rating: Rating
class CommentInput(BaseModel):
    uid: str
    comment: str
 
class ModelCommentInput(BaseModel):
    uid: str
    name: str
    question_id: str
    comment: CommentInput

class UserCreate(BaseModel):
    email: EmailStr
    username: constr(min_length=3, max_length=50)
    password: constr(min_length=6)

class UserLogin(BaseModel):
    identifier: str  # Can be either email or username
    password: str


#Translator vietnamese<->english
def vietnamese_to_english(text):
    translator = GoogleTranslator(source='vi', target='en')
    translated_text = translator.translate(text)
    return translated_text

# Hàm tạo XML format cho Moodle
def create_moodle_xml(questions):
    """Create Moodle XML from question list.

    Args:
        questions (list[dict]): list of questions.

    Returns:
        str: XML content as string.
    """
    quiz = ET.Element('quiz')

    for question in questions:
        question_el = ET.SubElement(quiz, 'question', type='multichoice')
        
        name_el = ET.SubElement(question_el, 'name')
        text_name_el = ET.SubElement(name_el, 'text')
        text_name_el.text = question['text']

        questiontext_el = ET.SubElement(question_el, 'questiontext', format='html')
        text_questiontext_el = ET.SubElement(questiontext_el, 'text')
        text_questiontext_el.text = f"<![CDATA[{question['text']}]]>"

        # Thêm các câu trả lời
        for answer in question['choices']:
            fraction = "100" if answer == question['correct_choice'] else "0"
            answer_el = ET.SubElement(question_el, 'answer', fraction=fraction)
            text_answer_el = ET.SubElement(answer_el, 'text')
            text_answer_el.text = answer
            feedback_el = ET.SubElement(answer_el, 'feedback')
            text_feedback_el = ET.SubElement(feedback_el, 'text')
            text_feedback_el.text = "Correct!" if answer == question['correct_choice'] else "Incorrect."

    # Tạo nội dung XML từ ElementTree
    xml_str = ET.tostring(quiz, encoding='unicode')
    return xml_str


# API
# req -> context and ans-s,
# res -> questions
@ app.post('/get-question')
async def model_inference(request: ModelInput, bg_task: BackgroundTasks, token: str = Depends(auth_scheme)):
    """Process user request

    Args:
        request (ModelInput): request model
        bg_task (BackgroundTasks): run process_request() on other thread
        and respond to request

    Returns:
        dict(str: int): response
    """
    # bg_task.add_task(process_request, request)


    # # Tạo một dictionary để lưu trữ kết quả
    # results = []

    # def background_task():
    #     nonlocal results
    #     results = process_request(request)

    # # Thêm tác vụ nền để xử lý yêu cầu
    # bg_task.add_task(background_task)


    # Thực hiện xử lý yêu cầu và lưu kết quả vào Firestore
    # Không dùng background vì để nó chạy trong cùng 1 thread để chờ xử lí xong mới có results
    results = process_request(request)

    return {
        'status': 200,
        'data': results
    }

# API để chia đoạn văn thành các câu và gửi yêu cầu cho API `get-question`
@ app.post('/get-questions')
async def get_questions(request: ModelInput, bg_task: BackgroundTasks, token: str = Depends(auth_scheme)):
    """Process user request by splitting the context into sentences 
    and sending requests to the `get-question` API for each sentence.

    Args:
        request (ModelInput): request model

    Returns:
        dict: response with status
    """
    # Khởi tạo danh sách kết quả trước khi vòng lặp bắt đầu
    results = []

    # Tạo biểu thức chính quy để tìm các dấu ngắt câu
    sentence_delimiters = r'[.!?]'

    # Tách đoạn văn thành các câu
    sentences = re.split(sentence_delimiters, request.context)

    # Loại bỏ các câu rỗng
    sentences = [sentence.strip() for sentence in sentences if sentence.strip()]

    # Gửi yêu cầu cho mỗi câu và thu thập kết quả
    for sentence in sentences:
        result = process_request(ModelInput(context=sentence, uid=request.uid, name=request.name))
        results.append(result)
        # bg_task.add_task(process_request, ModelInput(context=sentence, uid=request.uid, name=request.name))

    # Trả về kết quả
    return JSONResponse(content={'status': 200, 'data': results})

@ app.post('/export-questions')
async def export_questions(request: ModelExportInput):
    """Export questions in Aiken format based on the provided topic.

    Args:
        request (ModelExportInput): request model

    Returns:
        FileResponse: response with the exported file
    """
    try:
        questions = fs.get_questions_by_uid_and_topic(request.uid, request.name)  # Fetch questions from Firestore by uid and topic
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    aiken_format_content = ""

    for question in questions:
        aiken_format_content += f"{question['text']}\n"
        for idx, answer in enumerate(question['choices']):
            aiken_format_content += f"{chr(65 + idx)}. {answer}\n"
        correct_choice_index = question['choices'].index(question['correct_choice'])  # Lấy vị trí của đáp án đúng trong danh sách lựa chọn
        correct_choice = chr(65 + correct_choice_index)  # Convert số thành ký tự tương ứng
        aiken_format_content += f"ANSWER: {correct_choice}\n\n"

    # Đường dẫn đến thư mục Downloads của người dùng
    downloads_path = str(Path.home() / "Downloads")
    file_name = f"{request.name}.txt"
    file_path = os.path.join(downloads_path, file_name)

    with open(file_path, "w", encoding="utf-8") as file:
        file.write(aiken_format_content)

    return FileResponse(file_path, filename=file_name)

@ app.post('/export-questions-moodle')
async def export_questions_moodle(request: ModelExportInput):
    """Export questions in Moodle XML format based on the provided topic.

    Args:
        request (ModelExportInput): request model

    Returns:
        FileResponse: response with the exported file
    """
    try:
        questions = fs.get_questions_by_uid_and_topic(request.uid, request.name)  # Fetch questions from Firestore by uid and topic
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    
    moodle_xml_content = create_moodle_xml(questions)

    # Đường dẫn đến thư mục Downloads của người dùng
    downloads_path = str(Path.home() / "Downloads")
    file_name = f"{request.name}.xml"
    file_path = os.path.join(downloads_path, file_name)

    with open(file_path, "w", encoding="utf-8") as file:
        file.write(moodle_xml_content)

    return FileResponse(file_path, filename=file_name)

@ app.post('/duplicate-questions-answers')
async def get_duplicate_questions_answers(request: ModelExportInput, token: str = Depends(auth_scheme)):
    try:
        # Lấy danh sách các câu hỏi từ Firebase theo uid và chủ đề (name)
        questions = fs.get_questions_by_uid_and_topic(uid=request.uid, topic=request.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    duplicate_questions = []
    duplicate_answers = []

    # Kiểm tra các câu hỏi trùng nhau
    for idx1, q1 in enumerate(questions):
        for idx2, q2 in enumerate(questions[idx1 + 1:], start=idx1 + 1):
            if q1['text'] == q2['text']:
                duplicate_questions.append({'question': q1['text'], 'position1': idx1, 'position2': idx2})

            # Kiểm tra các đáp án trùng nhau
            for ans1 in q1['choices']:
                if ans1 in q2['choices']:
                    duplicate_answers.append({'answer': ans1, 'position1': idx1, 'position2': idx2})

    return {
        'duplicate_questions': duplicate_questions,
        'duplicate_answers': duplicate_answers
    }

@app.post('/rating-questions')
async def rate_questions(request: ModelRatingInput, token: str = Depends(auth_scheme)):
    try:
        # Tham chiếu đến tài liệu câu hỏi cụ thể
        doc_ref = fs._db.collection('users').document(request.uid).collection(request.name).document(request.question_id)
 
        # Lấy dữ liệu hiện tại của tài liệu
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            ratings = data.get('rating', [])
 
            # Kiểm tra xem uid đã tồn tại trong danh sách ratings chưa
            uid_exists = False
            for rating in ratings:
                if rating['uid'] == request.rating.uid:
                    rating['rate'] = request.rating.rate
                    uid_exists = True
                    break
 
            # Nếu uid không tồn tại, thêm mới vào danh sách ratings
            if not uid_exists:
                ratings.append({
                    'uid': request.rating.uid,
                    'rate': request.rating.rate
                })
 
            # Cập nhật trường rating trong Firestore
            doc_ref.update({'rating': ratings})
 
            # Tính toán điểm trung bình
            average_rating = sum(r['rate'] for r in ratings) / len(ratings) if ratings else 0
 
            # Cập nhật trường average_rating
            doc_ref.update({'average_rating': average_rating})
 
            return {
                'status': 200,
                'data': doc_ref.get().to_dict()  # Trả về dữ liệu đã cập nhật từ Firestore
            }
        else:
            raise ValueError("Question not found")
 
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")
 
   
@app.post('/comment-questions')
async def comment_questions(request: ModelCommentInput, token: str = Depends(auth_scheme)):
    try:
        # Tham chiếu đến tài liệu câu hỏi cụ thể
        doc_ref = fs._db.collection('users').document(request.uid).collection(request.name).document(request.question_id)
       
        # Lấy dữ liệu hiện tại của tài liệu
        doc = doc_ref.get()
        if doc.exists:
            data = doc.to_dict()
            comments = data.get('comments', [])
 
            # Thêm bình luận mới vào danh sách bình luận
            comments.append({
                'uid': request.comment.uid,
                'comment': request.comment.comment,
                'time': datetime.now().isoformat()  # Thêm thời gian hiện tại
            })
 
            # Cập nhật trường comments trong Firestore
            doc_ref.update({'comments': comments})
 
            return {
                'status': 200,
                'data': doc_ref.get().to_dict()  # Trả về dữ liệu đã cập nhật từ Firestore
            }
        else:
            raise ValueError("Question not found")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")
 
@app.get('/search-questions')
async def search_questions(keyword: str, token: str = Depends(auth_scheme)):
    try:
        # Tham chiếu đến bộ sưu tập người dùng
        users_collection = fs._db.collection('users')
        
        # Lấy tất cả các tài liệu người dùng
        users = users_collection.stream()

        matching_questions = []
        
        # Duyệt qua tất cả các tài liệu người dùng
        for user in users:
            user_id = user.id
            user_data = user.to_dict()
            
            # Duyệt qua tất cả các bộ sưu tập câu hỏi của mỗi người dùng
            question_collections = fs._db.collection('users').document(user_id).collections()
            for collection in question_collections:
                documents = collection.stream()
                
                # Lọc các câu hỏi dựa trên tiêu đề
                for doc in documents:
                    data = doc.to_dict()
                    if keyword.lower() in data['question'].lower():  # Tìm kiếm không phân biệt chữ hoa chữ thường
                        question_data = {
                            'user_id': user_id,
                            'collection_id': collection.id,
                            'id': doc.id,
                            'text': data['question'],
                            'choices': [data['all_ans'][str(i)] for i in range(4)],  # Giả sử có 4 lựa chọn
                            'correct_choice': data['crct_ans']
                        }
                        
                        # Chỉ thêm trường 'rating' nếu có ít nhất một đánh giá
                        if 'rating' in data and data['rating']:
                            question_data['rating'] = data['rating']
                        
                        matching_questions.append(question_data)

        return {
            'status': 200,
            'data': matching_questions
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")
 
@app.get('/delete-question')
async def delete_questions(uid: str, name: str, question_id: str, token: str = Depends(auth_scheme)):
    try:
        # Xóa toàn bộ bộ sưu tập câu hỏi của người dùng
        fs._db.collection('users').document(uid).collection(name).document(question_id).delete()
 
        return {
            'status': 200,
            'message': 'Question have been deleted successfully'
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")

# User registration
@app.post('/register')
async def register_user(user: UserCreate):
    """Register a new user with unique email and username validation.

    Args:
        user (UserCreate): user registration model

    Returns:
        JSONResponse: response with status
    """
    # Kiểm tra email duy nhất
    if fs.get_user_by_email(user.email):
        raise HTTPException(status_code=400, detail="Email already registered")

    # Kiểm tra username duy nhất
    if fs.get_user_by_username(user.username):
        raise HTTPException(status_code=400, detail="Username already taken")

    # Lưu người dùng mới vào Firestore
    user_data = fs.create_user(user.email, user.username, user.password)

    return JSONResponse(content={'status': 201, 'message': 'User registered successfully', 'user_data': user_data})

# User login
@app.post('/login')
async def login_user(user: UserLogin):
    """Login a user with email/username and password.

    Args:
        user (UserLogin): user login model

    Returns:
        JSONResponse: response with token
    """
    token, uid = fs.authenticate_user(user.identifier, user.password)

    return JSONResponse(content={'status': 200, 'token': token, 'uid': uid})
