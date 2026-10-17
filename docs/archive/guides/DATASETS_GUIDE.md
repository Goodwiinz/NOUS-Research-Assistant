# 📚 Dataset Integration Guide for RAG System

This guide provides detailed instructions for downloading, preparing, and integrating the example datasets mentioned in the project requirements.

## 🎯 **Recommended Datasets from project.txt**

### **1. DocVQA (Document Visual Question Answering)**
**🔗 Website**: https://docvqa.github.io/

**📋 What it is**: A dataset for document visual question answering, containing scanned document pages with questions and answers.

**📁 Types**: PDF documents with OCR text

**🔧 Why use it**:
- Perfect for testing PDF processing with OCR
- Contains real document layouts and formats
- Includes questions for evaluation testing
- Great for multimodal (text + image) testing

### **2. PubLayNet (Document Layout Analysis)**
**🔗 Website**: https://github.com/ibm-aur-nlp/PubLayNet

**📋 What it is**: Large dataset of scientific papers with layout annotations (titles, paragraphs, tables, figures).

**📁 Types**: Scientific papers (PDF) with structured layout information

**🔧 Why use it**:
- Excellent for testing complex document layouts
- Helps validate entity extraction from structured content
- Rich metadata for knowledge graph construction
- Real-world academic content for domain-specific testing

### **3. LAION-400M (Large-Scale Image Dataset)**
**🔗 Website**: https://laion.ai/blog/laion-400-open-dataset/

**📋 What it is**: 400 million image-text pairs for multimodal AI training.

**📁 Types**: Images with text captions/descriptions

**🔧 Why use it**:
- Massive dataset for testing image processing at scale
- Text captions for cross-modal entity linking
- Excellent for image-to-text search validation
- Helps test multimodal embedding generation

---

## 🚀 **Quick Start: Download and Setup**

### **Option 1: Download Sample Datasets (Recommended for Testing)**

#### **A. DocVQA Sample Dataset**
```bash
# Create dataset directory
mkdir -p datasets/docvqa

# Download a small sample (using a known mirror or sample)
# Note: DocVQA requires requesting access, so we'll use alternatives
echo "DocVQA requires manual download request. See: https://docvqa.github.io/"

# Alternative: Use HuggingFace datasets for similar content
pip install datasets

# Create a script to download similar document Q&A data
python3 -c "
from datasets import load_dataset
import json
import os

# Create sample dataset structure
os.makedirs('datasets/sample_documents', exist_ok=True)

# Sample documents for testing
sample_docs = [
    {
        'title': 'Artificial Intelligence Overview',
        'content': '''# Artificial Intelligence Overview

## Introduction
Artificial Intelligence (AI) represents the simulation of human intelligence in machines that are programmed to think and learn like humans. The field encompasses various subdomains:

### Machine Learning
Machine Learning is a subset of AI that enables systems to learn and improve from experience without being explicitly programmed. Key approaches include:

- **Supervised Learning**: Learning from labeled training data
- **Unsupervised Learning**: Finding patterns in unlabeled data
- **Reinforcement Learning**: Learning through interaction with environment
- **Deep Learning**: Neural networks with multiple layers

### Natural Language Processing
NLP focuses on enabling computers to understand, interpret, and generate human language. Applications include:
- Text classification and sentiment analysis
- Machine translation
- Question answering systems
- Text summarization

### Computer Vision
Computer Vision enables machines to interpret and understand visual information from the world:
- Image classification and object detection
- Face recognition
- Scene understanding
- Medical image analysis

## Applications
AI is transforming numerous industries:
- **Healthcare**: Disease diagnosis and drug discovery
- **Finance**: Fraud detection and algorithmic trading
- **Transportation**: Autonomous vehicles and route optimization
- **Retail**: Personalized recommendations and inventory management

## Future Trends
The future of AI includes:
- General Artificial Intelligence (AGI)
- Explainable AI (XAI)
- Edge AI and federated learning
- AI ethics and responsible AI development
''',
        'questions': [
            {'question': 'What is Machine Learning?', 'answer': 'Machine Learning is a subset of AI that enables systems to learn and improve from experience without being explicitly programmed.'},
            {'question': 'What are the main types of Machine Learning?', 'answer': 'Supervised Learning, Unsupervised Learning, Reinforcement Learning, and Deep Learning.'},
            {'question': 'What industries is AI transforming?', 'answer': 'Healthcare, Finance, Transportation, and Retail.'}
        ]
    }
]

# Save sample dataset
with open('datasets/sample_documents/ai_overview.json', 'w') as f:
    json.dump(sample_docs, f, indent=2)

print('Sample document dataset created successfully!')
"
```

#### **B. Create Scientific Papers Dataset**
```bash
# Create sample scientific papers
mkdir -p datasets/scientific_papers

# Download some open access papers or create samples
python3 -c "
import json
import os

# Sample scientific paper content
scientific_papers = [
    {
        'title': 'Deep Learning for Natural Language Processing',
        'authors': ['John Smith', 'Jane Doe'],
        'abstract': 'Recent advances in deep learning have revolutionized natural language processing. This paper explores transformer architectures, attention mechanisms, and their applications in machine translation, text summarization, and question answering. We present experimental results on benchmark datasets and discuss future research directions in multimodal AI systems.',
        'sections': [
            {
                'title': 'Introduction',
                'content': 'Natural language processing has evolved from rule-based systems to statistical models and now to neural networks. The introduction of transformer models marked a paradigm shift in how we approach sequence modeling tasks.'
            },
            {
                'title': 'Methodology',
                'content': 'We employ transformer architectures with multi-head attention mechanisms. Our models are trained on large-scale corpora using supervised learning objectives. We implement various NLP tasks including machine translation, text summarization, and question answering.'
            },
            {
                'title': 'Results',
                'content': 'Our models achieve state-of-the-art performance on GLUE, SuperGLUE, and WMT benchmarks. Transformer-based models show significant improvements over previous approaches in both accuracy and efficiency.'
            }
        ],
        'entities': [
            {'name': 'Transformer', 'type': 'concept'},
            {'name': 'Attention Mechanism', 'type': 'concept'},
            {'name': 'Machine Translation', 'type': 'concept'},
            {'name': 'Question Answering', 'type': 'concept'},
            {'name': 'GLUE', 'type': 'concept'},
            {'name': 'SuperGLUE', 'type': 'concept'}
        ]
    },
    {
        'title': 'Computer Vision in Autonomous Vehicles',
        'authors': ['Alice Johnson', 'Bob Wilson'],
        'abstract': 'Autonomous vehicles rely heavily on computer vision systems for navigation and obstacle detection. This comprehensive review covers state-of-the-art computer vision techniques used in self-driving cars, including object detection, semantic segmentation, and 3D scene understanding. We analyze current challenges and future research directions in real-time vision systems.',
        'sections': [
            {
                'title': 'Object Detection',
                'content': 'Modern autonomous vehicles use advanced object detection algorithms based on convolutional neural networks. YOLO, Faster R-CNN, and SSD architectures provide real-time detection capabilities essential for safe navigation.'
            },
            {
                'title': 'Semantic Segmentation',
                'content': 'Semantic segmentation provides pixel-level understanding of the driving environment. Deep learning models classify each pixel into categories like road, sidewalk, vehicle, pedestrian, and building.'
            },
            {
                'title': '3D Scene Understanding',
                'content': 'LiDAR and stereo vision systems create 3D representations of the environment. Point cloud processing enables depth estimation and spatial reasoning for complex traffic scenarios.'
            }
        ],
        'entities': [
            {'name': 'YOLO', 'type': 'concept'},
            {'name': 'Faster R-CNN', 'type': 'concept'},
            {'name': 'LiDAR', 'type': 'concept'},
            {'name': 'Autonomous Vehicles', 'type': 'concept'},
            {'name': 'Object Detection', 'type': 'concept'}
        ]
    }
]

# Save scientific papers dataset
with open('datasets/scientific_papers/papers.json', 'w') as f:
    json.dump(scientific_papers, f, indent=2)

print('Scientific papers dataset created successfully!')
"
```

#### **C. Create Image Dataset with Captions**
```bash
# Create image dataset with text descriptions
mkdir -p datasets/image_dataset

python3 -c "
import json
import os

# Sample image dataset with descriptions
image_dataset = [
    {
        'filename': 'meeting_room.jpg',
        'description': 'A modern conference room with a large wooden table in the center. Six people are seated around the table participating in a business meeting. The room has large windows on one wall allowing natural light to enter. There\'s a whiteboard on the wall with diagrams and notes. The atmosphere appears professional and collaborative.',
        'entities': [
            {'name': 'Conference Room', 'type': 'location'},
            {'name': 'Business Meeting', 'type': 'concept'},
            {'name': 'Whiteboard', 'type': 'object'}
        ],
        'tags': ['meeting', 'business', 'conference', 'professional']
    },
    {
        'filename': 'laboratory.jpg',
        'description': 'A scientific laboratory with various research equipment. Multiple workstations with computers and microscopes are visible. Glass cabinets contain chemical reagents and lab supplies. The lighting is bright and sterile, typical of a research environment. Safety equipment like eye wash stations are visible.',
        'entities': [
            {'name': 'Laboratory', 'type': 'location'},
            {'name': 'Microscope', 'type': 'object'},
            {'name': 'Research Equipment', 'type': 'concept'}
        ],
        'tags': ['science', 'research', 'laboratory', 'equipment']
    },
    {
        'filename': 'office_building.jpg',
        'description': 'A modern office building exterior with glass facade. Multiple floors are visible with people working at their desks. The building has a contemporary architectural design with clean lines and large windows. Green landscaping surrounds the building including trees and well-maintained lawns.',
        'entities': [
            {'name': 'Office Building', 'type': 'location'},
            {'name': 'Glass Facade', 'type': 'object'},
            {'name': 'Architecture', 'type': 'concept'}
        ],
        'tags': ['office', 'building', 'architecture', 'modern']
    }
]

# Save image dataset
with open('datasets/image_dataset/descriptions.json', 'w') as f:
    json.dump(image_dataset, f, indent=2)

print('Image dataset with descriptions created successfully!')
"
```

### **Option 2: Download Real Datasets (For Production)**

#### **A. Install Required Dependencies**
```bash
# Install dataset downloading tools
pip install datasets requests beautifulsoup4

# Install image downloading
pip install wget pillow

# Install document processing
pip install PyMuPDF pdfplumber pytesseract
```

#### **B. Download LAION-400M Sample**
```python
from datasets import load_dataset
import os

# Create dataset directory
os.makedirs('datasets/laion_sample', exist_ok=True)

# Load a small sample of LAION dataset
print("Loading LAION dataset sample...")
dataset = load_dataset('laion/laion400m-music', split='train', streaming=True)

# Save first 1000 examples
sample_data = []
for i, example in enumerate(dataset):
    if i >= 1000:  # Limit to 1000 samples for testing
        break
    sample_data.append({
        'id': example['id'],
        'caption': example['caption'],
        'url': example['url'],
        'similarity': example['similarity']
    })

# Save sample dataset
import json
with open('datasets/laion_sample/laion_1000.json', 'w') as f:
    json.dump(sample_data, f, indent=2)

print(f"Downloaded {len(sample_data)} samples from LAION dataset")
```

#### **C. Download ArXiv Papers (Alternative to PubLayNet)**
```python
import requests
import json
import os
from datetime import datetime, timedelta

# Create dataset directory
os.makedirs('datasets/arxiv_papers', exist_ok=True)

# Function to download ArXiv papers
def download_arxiv_papers(query="machine learning", max_results=50):
    base_url = "http://export.arxiv.org/api/query"

    # Calculate date range (last 30 days)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)

    params = {
        'search_query': f'all:{query}',
        'start': 0,
        'max_results': max_results,
        'sortBy': 'submittedDate',
        'sortOrder': 'descending'
    }

    try:
        response = requests.get(base_url, params=params)
        response.raise_for_status()

        return response.json()
    except Exception as e:
        print(f"Error downloading ArXiv papers: {e}")
        return None

# Download papers
papers_data = download_arxiv_papers("artificial intelligence", 50)

if papers_data and 'entries' in papers_data:
    processed_papers = []

    for entry in papers_data['entries']:
        paper = {
            'id': entry['id'].split('/')[-1],
            'title': entry['title'],
            'authors': [author['name'] for author in entry['authors']],
            'abstract': entry['summary'],
            'published': entry['published'],
            'categories': entry['categories'],
            'links': {
                'pdf': entry.get('link', [{}])[0].get('href', ''),
                'doi': entry.get('arxiv_doi', '')
            }
        }
        processed_papers.append(paper)

    # Save processed papers
    with open('datasets/arxiv_papers/ai_papers.json', 'w') as f:
        json.dump(processed_papers, f, indent=2)

    print(f"Downloaded {len(processed_papers)} ArXiv papers")
```

---

## 🔧 **Dataset Integration with Your RAG System**

### **Step 1: Create Dataset Upload Script**
```bash
# Create dataset upload script
cat > scripts/upload_datasets.py << 'EOF'
"""
Script to upload sample datasets to the RAG system
"""

import json
import requests
import os
from pathlib import Path

# Configuration
BASE_URL = "http://localhost:8000"
UPLOAD_URL = f"{BASE_URL}/api/documents/upload"

# Login credentials (use your test user credentials)
LOGIN_URL = f"{BASE_URL}/api/auth/login"
LOGIN_DATA = {
    "email": "test@example.com",
    "password": "SecurePass123!"
}

def login():
    """Login to get auth token"""
    response = requests.post(LOGIN_URL, json=LOGIN_DATA)
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        raise Exception(f"Login failed: {response.status_code}")

def upload_document(file_path, title, description, token):
    """Upload a document to the RAG system"""
    headers = {"Authorization": f"Bearer {token}"}

    with open(file_path, 'rb') as f:
        files = {"file": (os.path.basename(file_path), f, 'application/octet-stream')}
        data = {
            "title": title,
            "description": description
        }

        response = requests.post(UPLOAD_URL, files=files, data=data, headers=headers)
        return response

def main():
    """Main upload function"""
    print("📚 Starting dataset upload to RAG system...")

    # Login
    try:
        token = login()
        print("✅ Successfully logged in")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return

    # Upload sample documents
    datasets_to_upload = [
        {
            'file_path': 'datasets/sample_documents/ai_overview.json',
            'title': 'AI Overview Document',
            'description': 'Comprehensive overview of artificial intelligence concepts and applications'
        },
        {
            'file_path': 'datasets/scientific_papers/papers.json',
            'title': 'Scientific Papers Collection',
            'description': 'Collection of academic papers on AI and computer vision topics'
        },
        {
            'file_path': 'datasets/image_dataset/descriptions.json',
            'title': 'Image Descriptions Dataset',
            'description': 'Text descriptions and metadata for image dataset'
        }
    ]

    successful_uploads = 0

    for dataset in datasets_to_upload:
        if os.path.exists(dataset['file_path']):
            try:
                response = upload_document(
                    dataset['file_path'],
                    dataset['title'],
                    dataset['description'],
                    token
                )

                if response.status_code in [200, 201]:
                    result = response.json()
                    print(f"✅ Uploaded: {dataset['title']}")
                    print(f"   Document ID: {result.get('id')}")
                    successful_uploads += 1
                else:
                    print(f"❌ Upload failed for {dataset['title']}: {response.status_code}")
                    print(f"   Error: {response.text}")

            except Exception as e:
                print(f"❌ Error uploading {dataset['title']}: {e}")
        else:
            print(f"⚠️ File not found: {dataset['file_path']}")

    print(f"\n🎉 Upload Summary:")
    print(f"   Successfully uploaded: {successful_uploads}/{len(datasets_to_upload)} datasets")
    print(f"   System URL: {BASE_URL}")

if __name__ == "__main__":
    main()
EOF

# Make script executable
chmod +x scripts/upload_datasets.py
```

### **Step 2: Create Document Conversion Script**
```bash
# Create document conversion script
cat > scripts/convert_datasets.py << 'EOF'
"""
Script to convert JSON datasets to actual documents (PDF/TXT)
"""

import json
import os
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

def convert_json_to_pdf(json_file, output_dir):
    """Convert JSON dataset to PDF document"""

    # Read JSON data
    with open(json_file, 'r') as f:
        data = json.load(f)

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Convert each item to PDF
    for i, item in enumerate(data):
        filename = f"{item['title'].replace(' ', '_').lower()}.pdf"
        filepath = os.path.join(output_dir, filename)

        # Create PDF
        doc = SimpleDocTemplate(filepath, pagesize=letter)
        styles = getSampleStyleSheet()
        story = []

        # Add title
        title_style = styles['Heading1']
        story.append(Paragraph(item['title'], title_style))
        story.append(Spacer(1, 12))

        # Add content
        if 'content' in item:
            content_style = styles['Normal']
            content_text = item['content']

            # Split content into paragraphs
            paragraphs = content_text.split('\n\n')
            for para in paragraphs:
                if para.strip():
                    story.append(Paragraph(para, content_style))
                    story.append(Spacer(1, 6))

        # Add questions if available
        if 'questions' in item:
            story.append(Spacer(1, 12))
            qa_style = styles['Heading2']
            story.append(Paragraph("Questions and Answers", qa_style))
            story.append(Spacer(1, 6))

            for qa in item['questions']:
                q_style = styles['Heading3']
                a_style = styles['Normal']

                story.append(Paragraph(f"Q: {qa['question']}", q_style))
                story.append(Paragraph(f"A: {qa['answer']}", a_style))
                story.append(Spacer(1, 6))

        # Build PDF
        doc.build(story)
        print(f"✅ Created PDF: {filepath}")

def main():
    """Main conversion function"""
    print("🔄 Converting JSON datasets to PDF documents...")

    # Convert sample documents
    datasets_dir = Path("datasets")

    sample_docs = datasets_dir / "sample_documents"
    if sample_docs.exists():
        json_file = sample_docs / "ai_overview.json"
        if json_file.exists():
            output_dir = datasets_dir / "pdf_documents"
            convert_json_to_pdf(json_file, output_dir)

    # Convert scientific papers
    scientific_papers = datasets_dir / "scientific_papers"
    if scientific_papers.exists():
        json_file = scientific_papers / "papers.json"
        if json_file.exists():
            output_dir = datasets_dir / "scientific_pdfs"
            convert_json_to_pdf(json_file, output_dir)

    print("✅ Dataset conversion completed!")

if __name__ == "__main__":
    main()
EOF

# Make script executable
chmod +x scripts/convert_datasets.py
```

### **Step 3: Create Test Query Set**
```bash
# Create test queries based on the datasets
cat > datasets/test_queries.json << 'EOF'
{
  "test_queries": [
    {
      "query": "What is machine learning?",
      "expected_entities": ["Machine Learning", "AI", "Supervised Learning"],
      "modality": "text",
      "difficulty": "easy"
    },
    {
      "query": "How does transformer architecture work?",
      "expected_entities": ["Transformer", "Attention Mechanism", "Neural Network"],
      "modality": "text",
      "difficulty": "medium"
    },
    {
      "query": "What are the applications of computer vision in autonomous vehicles?",
      "expected_entities": ["Computer Vision", "Autonomous Vehicles", "Object Detection"],
      "modality": "text",
      "difficulty": "medium"
    },
    {
      "query": "Find information about research laboratories and scientific equipment",
      "expected_entities": ["Laboratory", "Microscope", "Research Equipment"],
      "modality": "image",
      "difficulty": "easy"
    },
    {
      "query": "What does a modern conference room look like?",
      "expected_entities": ["Conference Room", "Meeting", "Business"],
      "modality": "image",
      "difficulty": "easy"
    },
    {
      "query": "Deep learning approaches for natural language processing",
      "expected_entities": ["Deep Learning", "NLP", "Transformer"],
      "modality": "text",
      "difficulty": "hard"
    }
  ],
  "evaluation_criteria": {
    "answer_relevancy_threshold": 0.7,
    "contextual_precision_threshold": 0.6,
    "factual_accuracy_threshold": 0.8,
    "response_time_threshold_ms": 2000
  }
}
EOF
```

---

## 🧪 **Testing with Downloaded Datasets**

### **Step 4: Create Evaluation Script**
```bash
# Create evaluation script
cat > scripts/evaluate_datasets.py << 'EOF'
"""
Script to evaluate RAG system performance with the test datasets
"""

import json
import requests
import time
from datetime import datetime

# Configuration
BASE_URL = "http://localhost:8000"
SEARCH_URL = f"{BASE_URL}/api/search/hybrid"

# Login credentials
LOGIN_URL = f"{BASE_URL}/api/auth/login"
LOGIN_DATA = {
    "email": "test@example.com",
    "password": "SecurePass123!"
}

def login():
    """Login to get auth token"""
    response = requests.post(LOGIN_URL, json=LOGIN_DATA)
    if response.status_code == 200:
        return response.json()["access_token"]
    else:
        raise Exception(f"Login failed: {response.status_code}")

def evaluate_query(query, expected_entities, token):
    """Evaluate a single search query"""
    headers = {"Authorization": f"Bearer {token}"}

    search_data = {
        "query": query,
        "limit": 10,
        "search_type": "hybrid"
    }

    start_time = time.time()
    response = requests.post(SEARCH_URL, json=search_data, headers=headers)
    end_time = time.time()

    if response.status_code == 200:
        results = response.json()

        # Calculate metrics
        response_time_ms = (end_time - start_time) * 1000

        # Check if expected entities are found
        found_entities = []
        if 'results' in results:
            for result in results['results']:
                result_text = result.get('content', '').lower()
                for entity in expected_entities:
                    if entity.lower() in result_text:
                        found_entities.append(entity)

        entity_recall = len(found_entities) / len(expected_entities) if expected_entities else 0

        return {
            'query': query,
            'response_time_ms': response_time_ms,
            'results_count': len(results.get('results', [])),
            'found_entities': found_entities,
            'expected_entities': expected_entities,
            'entity_recall': entity_recall,
            'success': True
        }
    else:
        return {
            'query': query,
            'error': f"Search failed: {response.status_code}",
            'success': False
        }

def main():
    """Main evaluation function"""
    print("🧪 Starting RAG System Evaluation with Test Datasets")

    # Load test queries
    with open('datasets/test_queries.json', 'r') as f:
        test_data = json.load(f)

    # Login
    try:
        token = login()
        print("✅ Successfully logged in")
    except Exception as e:
        print(f"❌ Login failed: {e}")
        return

    # Evaluate each query
    results = []
    criteria = test_data['evaluation_criteria']

    print(f"\\n📊 Evaluating {len(test_data['test_queries'])} queries...")

    for i, test_case in enumerate(test_data['test_queries'], 1):
        print(f"\\n{i}. Query: {test_case['query']}")
        print(f"   Difficulty: {test_case['difficulty']}")
        print(f"   Modality: {test_case['modality']}")

        result = evaluate_query(
            test_case['query'],
            test_case['expected_entities'],
            token
        )

        if result['success']:
            print(f"   ✅ Results: {result['results_count']}")
            print(f"   ⏱️  Response Time: {result['response_time_ms']:.0f}ms")
            print(f"   🎯 Entity Recall: {result['entity_recall']:.2f}")
            print(f"   📋 Found Entities: {', '.join(result['found_entities'])}")

            # Check if meets thresholds
            meets_time_threshold = result['response_time_ms'] <= criteria['response_time_threshold_ms']
            meets_recall_threshold = result['entity_recall'] >= criteria['entity_recall_threshold']

            print(f"   ✅ Time Threshold: {'PASS' if meets_time_threshold else 'FAIL'}")
            print(f"   ✅ Recall Threshold: {'PASS' if meets_recall_threshold else 'FAIL'}")
        else:
            print(f"   ❌ Error: {result['error']}")

        results.append(result)

    # Calculate overall metrics
    successful_queries = [r for r in results if r['success']]
    if successful_queries:
        avg_response_time = sum(r['response_time_ms'] for r in successful_queries) / len(successful_queries)
        avg_entity_recall = sum(r['entity_recall'] for r in successful_queries) / len(successful_queries)

        print(f"\\n📈 Overall Results:")
        print(f"   Queries Tested: {len(test_data['test_queries'])}")
        print(f"   Successful Queries: {len(successful_queries)}")
        print(f"   Success Rate: {(len(successful_queries)/len(test_data['test_queries']))*100:.1f}%")
        print(f"   Average Response Time: {avg_response_time:.0f}ms")
        print(f"   Average Entity Recall: {avg_entity_recall:.2f}")

        # Check overall success
        meets_time_criteria = avg_response_time <= criteria['response_time_threshold_ms']
        meets_recall_criteria = avg_entity_recall >= criteria['entity_recall_threshold']

        print(f"\\n🎯 Threshold Compliance:")
        print(f"   Response Time: {'✅ PASS' if meets_time_criteria else '❌ FAIL'} ({criteria['response_time_threshold_ms']}ms threshold)")
        print(f"   Entity Recall: {'✅ PASS' if meets_recall_criteria else '❌ FAIL'} ({criteria['entity_recall_threshold']:.0f} threshold)")

        if meets_time_criteria and meets_recall_criteria:
            print(f"\\n🎉 Overall Evaluation: ✅ PASS")
        else:
            print(f"\\n⚠️  Overall Evaluation: ❌ NEEDS IMPROVEMENT")

    else:
        print(f"\\n❌ All queries failed!")

if __name__ == "__main__":
    main()
EOF

# Make script executable
chmod +x scripts/evaluate_datasets.py
```

---

## 📋 **Complete Workflow**

### **1. Setup and Download**
```bash
# 1. Create directories
mkdir -p datasets/{sample_documents,scientific_papers,image_dataset,pdf_documents}
mkdir -p scripts

# 2. Create sample datasets (run scripts from above)
python3 -c "# Run sample dataset creation scripts from above"

# 3. Convert to PDF (optional)
pip install reportlab
python3 scripts/convert_datasets.py

# 4. Upload to RAG system
python3 scripts/upload_datasets.py
```

### **2. Test and Evaluate**
```bash
# 1. Start RAG system
docker-compose up -d

# 2. Run evaluation
python3 scripts/evaluate_datasets.py
```

### **3. Analyze Results**
- 📊 Check search result quality
- 🎯 Validate entity extraction accuracy
- ⏱️ Measure response time performance
- 🔍 Test cross-modal search capabilities

---

## 🎯 **Dataset Recommendations**

### **For Testing/Development:**
- ✅ **Sample JSON datasets** (quick to create, easy to control)
- ✅ **ArXiv papers** (real academic content)
- ✅ **Custom synthetic data** (specific to your use case)

### **For Production/Demo:**
- 🌟 **DocVQA** (document Q&A with real PDFs)
- 📚 **PubLayNet** (scientific papers with layout analysis)
- 🖼️ **LAION-400M sample** (large image-text pairs)

### **For Specialized Testing:**
- 🏢 **Business documents** (invoices, reports, contracts)
- 📖 **Educational content** (textbooks, course materials)
- 🏥 **Medical documents** (research papers, clinical notes)
- 🔬 **Technical documentation** (API docs, manuals)

This comprehensive guide will help you create realistic test datasets that align perfectly with your RAG system's capabilities and evaluation requirements! 🚀