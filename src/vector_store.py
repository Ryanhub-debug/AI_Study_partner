import logging
import json
from pathlib import Path
from typing import List, Any
from langchain_community.document_loaders import PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_ollama import ChatOllama
from langchain.schema import Document
from langchain_community.document_loaders import WebBaseLoader
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_community.retrievers import TFIDFRetriever
import bs4

# Constants
model = "qwen2.5-coder:3b"
llm = ChatOllama(model=model)
VECTOR_STORE_PATH = Path("faiss_index")


class FallbackTFIDFRetriever(BaseRetriever):
    documents: List[Document] = []
    k: int = 4
    _tfidf_retriever: Any = None

    class Config:
        arbitrary_types_allowed = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._update_retriever()

    def _update_retriever(self):
        if self.documents:
            self._tfidf_retriever = TFIDFRetriever.from_documents(self.documents)
            self._tfidf_retriever.k = self.k
        else:
            self._tfidf_retriever = None

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun = None
    ) -> List[Document]:
        if self._tfidf_retriever is not None:
            return self._tfidf_retriever.invoke(query)
        return []


class VectorStore:
    """
    A class for managing a local document store using a TF-IDF keyword-based index.
    """
    def __init__(self, vector_store_path=VECTOR_STORE_PATH, llm_model="qwen2.5-coder:3b",
                  chunk_size=500, chunk_overlap=50, persist=True, index_path="faiss_index"):
        self.vector_store_path = Path(vector_store_path)
        self.llm_model = llm_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.persist = persist
        
        self.index_path = Path(index_path)
        if self.index_path.is_dir() or not self.index_path.suffix:
            self.index_file = self.index_path / "documents.json"
        else:
            self.index_file = self.index_path
            
        self.documents = []
        self._setup_vector_store()

    def _setup_vector_store(self) -> None:
        if self.index_file.exists():
            try:
                with open(self.index_file, "r", encoding="utf-8") as f:
                    docs_data = json.load(f)
                self.documents = [
                    Document(page_content=d["page_content"], metadata=d["metadata"])
                    for d in docs_data
                ]
                print(f"Loaded {len(self.documents)} documents from {self.index_file}")
            except Exception as e:
                print(f"Error loading documents: {e}")
                self.documents = []
        else:
            print(f"No documents file found at {self.index_file}")
            self.documents = []
            
        self.retriever = FallbackTFIDFRetriever(documents=self.documents)

    def load_documents(self, data_path) -> List[Document]:
        documents = []
        for pdf_path in Path(data_path).glob("*.pdf"):
            docs = self.load_document(pdf_path)
            print(len(docs))
            documents.extend(docs)
        return documents
    
    def add_documents(self, documents: List[Document]) -> List[Document]:
        splitted_docs = self.chunk_documents(documents=documents)
        self.documents.extend(splitted_docs)
        self.retriever.documents = self.documents
        self.retriever._update_retriever()
        if self.persist:
            try:
                self.index_file.parent.mkdir(parents=True, exist_ok=True)
                docs_data = [{"page_content": doc.page_content, "metadata": doc.metadata} for doc in self.documents]
                with open(self.index_file, "w", encoding="utf-8") as f:
                    json.dump(docs_data, f, ensure_ascii=False, indent=2)
                print(f"Saved {len(self.documents)} documents to {self.index_file}")
            except Exception as e:
                print(f"Error saving documents: {e}")
        return splitted_docs

    def chunk_documents(self, documents: List[Document]) -> List[Document]:
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=self.chunk_size, 
                                              chunk_overlap=self.chunk_overlap)    
        return text_splitter.split_documents(documents)
    
    def add_all_documents(self, data_path: str = "data") -> List[Document]:
        docs = self.load_documents(data_path)
        return self.add_documents(docs)
    
    def load_document(self, pdf_path: Path) -> List[Document]:
        loader = PyPDFLoader(str(pdf_path))
        docs = loader.load()
        return docs if isinstance(docs, list) else [docs]
    
    def add_document(self, filePath: Path) -> List[Document]:
        docs = self.load_document(filePath)
        return self.add_documents(docs)
    
    def similarity_search(self, question: str, k:int) -> List[Document]:
        old_k = self.retriever.k
        self.retriever.k = k
        self.retriever._update_retriever()
        results = self.retriever.invoke(question)
        self.retriever.k = old_k
        self.retriever._update_retriever()
        return results
    
    def as_retriever(self) -> BaseRetriever:
        return self.retriever

    def index_websites(self, urls: list[str]) -> List[Document]:
        docs = self.website_to_documents(urls)
        return self.add_documents(docs)

    def website_to_documents(self, urls: list[str]) -> list[Document]:
        loader = WebBaseLoader(
            web_paths=urls,
            bs_kwargs=dict(
                parse_only=bs4.SoupStrainer(
                    class_=("post-content", "post-title", "post-header")
                )
            ),
        )
        docs = loader.load()
        return docs