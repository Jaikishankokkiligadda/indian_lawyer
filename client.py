import requests
import streamlit as st

def get_response(input_text):
    response = requests.post(
        "http://localhost:8000/lawyer/invoke",
        json={"input": {"question": input_text}}
    )
    return response.json()["output"]

st.title("Indian Lawyer Assistant")

input_text = st.text_input("Enter your legal question:")

if input_text:
    result = get_response(input_text)
    st.write(result) 