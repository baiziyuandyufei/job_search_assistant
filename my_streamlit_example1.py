import streamlit as st
from dotenv import load_dotenv
from langchain.text_splitter import RecursiveCharacterTextSplitter, CharacterTextSplitter
from langchain.document_loaders import WebBaseLoader
from langchain_fireworks import FireworksEmbeddings, ChatFireworks
from langchain.vectorstores import FAISS
from langchain_core.prompts import (
    FewShotPromptTemplate,
    PromptTemplate,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
    ChatPromptTemplate,
    PromptTemplate)
from langchain_core.runnables import (
    RunnableParallel,
    RunnablePassthrough,
    RunnableLambda)
from langchain_core.output_parsers import StrOutputParser
import re
import logging


# 配置日志
logging.basicConfig(
    format='%(asctime)s %(levelname)s %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler()
    ]
)
# 获取日志记录器
logger = logging.getLogger(__name__)

# 部署到streamlit时，请在streamlit中配置环境变量
load_dotenv()

# # 直接在info括号内获取并输出环境变量的值
# logging.info(f'LANGCHAIN_TRACING_V2: {os.getenv("LANGCHAIN_TRACING_V2", "未设置")}')
# logging.info(f'LANGCHAIN_ENDPOINT: {os.getenv("LANGCHAIN_ENDPOINT", "未设置")}')
# logging.info(f'LANGCHAIN_API_KEY: {os.getenv("LANGCHAIN_API_KEY", "未设置")}')
# logging.info(f'LANGCHAIN_PROJECT: {os.getenv("LANGCHAIN_PROJECT", "未设置")}')


class JobSearchAssistant:
    def __init__(self, url, embedding_model_name, chat_model_name):
        # 文档加载、分割
        self.loader = WebBaseLoader(url)
        self.raw_documents = self.loader.load()
        self.text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=100,
            chunk_overlap=20,
            length_function=len,
            add_start_index=False,
        )
        self.documents = self.text_splitter.split_documents(self.raw_documents)
        print(f"分割后快数: {len(self.documents)}")

        # 向量化、存储
        self.embedding_model = FireworksEmbeddings(model=embedding_model_name)
        self.db = FAISS.from_documents(
            documents=self.documents, embedding=self.embedding_model)
        print(f"索引片段数: {self.db.index.ntotal}")

        # 检索器
        self.retriever = self.db.as_retriever()

        # 检索链
        self.question_retrieval_chain = self.retriever | RunnableLambda(
            lambda docs: "\n".join([doc.page_content for doc in docs]))

        # 实例化聊天模型
        self.chat = ChatFireworks(
            model=chat_model_name, temperature=0.3, top_p=0.3)

        # 分类词典
        self.question_classify_dict = {
            "离职原因": {
                "response": "有换工意愿，上家公司离我居住地太远，通勤时间太长。",
                "examples": [{"text": "离职/换工作的原因", "label": "离职原因"}]
            },
            "薪资": {
                "response": "我期望薪资为30K～40K。",
                "examples": [{"text": "但是我们应该最高30K，一般还达不到.", "label": "薪资"}]
            },
            "外包&外协&外派&驻场": {
                "response": "请发送或说明职位的办公地点定位。以及薪资范围。我期望薪资范围30-40K？",
                "examples": [{"text": "你好，我们是外协岗位，在国家电网 南瑞工作的", "label": "外包&外协&外派&驻场"}]
            },
            "兼职": {
                "response": "职位的办公地点在哪？薪资多少，怎么结算？",
                "examples": [{"text": "哈喽～本职位为线上兼职，一单一结款，根据自己时间自由接单，不耽误自己的主业，您看感兴趣嘛？", "label": "兼职"}]
            },
            "预约面试": {
                "response": "本周内上午、下午都有时间。",
                "examples": [{"text": "想约您面试，方便的话麻烦告诉我一下您可以约面试的日期及时间【请选择工作日内的上午10-12点或下午14点到17点内的时间】。", "label": "预约面试"}]
            },
            "到岗时间": {
                "response": "两周内到岗。",
                "examples": [{"text": "咱到岗时间呢。", "label": "到岗时间"}]
            },
            "其他": {
                "response": "",
                "examples": []
            }
        }

        # 构建分类提示
        self.examples, self.example_prompt, self.prefix, self.suffix = self.prepare_question_classify_prompt()
        self.few_shot_prompt = FewShotPromptTemplate(
            examples=self.examples,
            example_prompt=self.example_prompt,
            prefix=self.prefix,
            suffix=self.suffix,
            input_variables=["input"],
            example_separator="\n"
        )

        # 分类链
        self.question_classify_chain = self.few_shot_prompt | self.chat | StrOutputParser(
        ) | RunnableLambda(self.label_to_response)

        # 系统提示
        self.system_message_prompt = SystemMessagePromptTemplate.from_template(f"""
        你是求职助手于先生。刚从中国科学院信息工程研究所离职。居住地为北京。
        """)

        # 人工提示
        self.human_message_prompt = HumanMessagePromptTemplate.from_template("""
        HR问或说: {question}。

        {context}

        请用汉语回复内容，内容的头部和尾部不要出现引号。
        """)

        # 合并链提示
        self.merge_prompt = PromptTemplate.from_template("""
        你在回答中体现以下内容

        {question_classify_response}

        你的工作经历有以下内容，如果这些内容与HR的问题有关请考虑

        {question_retrieval_response}
        """)

        # 整体链
        self.final_chain = {
            "question": RunnablePassthrough(),
            "context": RunnableParallel(question_classify_response=self.question_classify_chain,
                                        question_retrieval_response=self.question_retrieval_chain) | self.merge_prompt
        } | ChatPromptTemplate.from_messages([self.system_message_prompt, self.human_message_prompt]) | self.chat | StrOutputParser()

    def prepare_question_classify_prompt(self):
        examples = []
        for key in self.question_classify_dict:
            r_examples = self.question_classify_dict[key]["examples"]
            if len(r_examples) > 0:
                examples.extend(r_examples)

        example_prompt = PromptTemplate.from_template(
            """文本: {text}
            类别: {label}
            """
        )

        prefix = f"""
        给出每个文本的类别，类别只能属于以下列出的一种

        {"- ".join(self.question_classify_dict.keys())}

        如果不属于以上类别，则类别名称为“其他”。

        例如：
        """

        suffix = """文本: {input}\n类别:
        """
        return examples, example_prompt, prefix, suffix

    def label_to_response(self, label):
        label = re.sub('类别: ?', '', label)
        label = label if label in self.question_classify_dict else "其他"
        response = self.question_classify_dict[label]["response"]
        return response

    def get_response(self, question):
        return self.final_chain.invoke(question)


url = "https://raw.githubusercontent.com/baiziyuandyufei/langchain-self-study-tutorial/main/jl.txt"
embedding_model_name = "nomic-ai/nomic-embed-text-v1.5"
chat_model_name = "accounts/fireworks/models/llama-v3-70b-instruct"

assistant = JobSearchAssistant(url, embedding_model_name, chat_model_name)

# 人机交互界面

# 页面大标题
st.title("个人求职助手")
st.title("💬 聊天机器人")
# 页面描述
st.caption("🚀 一个Streamlit个人求职助手聊天机器人，基于FireWorks的llama-v3-70b-instruct模型")
# 侧边栏
with st.sidebar:
    st.markdown("""
    ## 开发计划

    ### 1. 求职助手

    #### 目的

    代表用户同HR交流

    #### 开发计划

    - [x] [整体链结构设计](https://github.com/baiziyuandyufei/langchain-self-study-tutorial/blob/main/LCEL.ipynb)
    - [x] [通用聊天模板设计]()
    - [x] [问题分类链设计：问题分类FewShot模板设计]()
    - [x] [问题检索链设计：简历知识库构建-嵌入模型选择、向量存储、检索]()
    - [x] 人机交互界面开发
    - [ ] 距离工具，输入职位地点后，自动匹配距离，远距离不去。
    - [ ] 岗位职责描述与自身条件满足度得分。

    #### 当前进度

    - 合并问题分类链+问题检索链，生成整体链。
    - 选择合适在线向量库。

    ### 2. 快速生成简历

    #### 目的

    根据不同JD描述，生成适配的简历内容

    #### 填写项

    用户填表，点击提交后，生成简历。

    - 岗位职责：
    - 岗位描述：
    - 学历信息：
    - 工作经历：
    -- 开发语言
    -- 工具库
    -- 模型
    -- 其他
    - 离职原因：
    - 现居住地：

    """)

# 初始化聊天消息会话
if "messages" not in st.session_state:
    #  添加助手消息
    st.session_state["messages"] = [
        {"role": "assistant", "content": "我是你的个人求职助手，帮你回答HR提出的问题，你可以将HR的问题输入给我！"}]

# 显示会话中的所有聊天消息
for msg in st.session_state.messages:
    st.chat_message(msg["role"]).write(msg["content"])

# 聊天输入表格
# 这句代码使用了海象运算符，将用户在聊天输入框中输入的内容赋值给变量prompt，并检查这个输入内容是否为真（即是否有输入内容）。
if prompt := st.chat_input("HR的问题"):
    logger.info(f"用户输入: {prompt}")
    # 向会话消息中添加用户输入
    st.session_state.messages.append({"role": "user", "content": prompt})
    # 显示用户输入
    st.chat_message("user").write(prompt)
    # 调用链获取响应
    response = assistant.get_response(prompt)
    logger.info(f"AI响应: {response}")
    # 向会话消息中添加助手输入
    st.session_state.messages.append(
        {"role": "assistant", "content": response})
    # 显示助手消息
    st.chat_message("assistant").write(response)
