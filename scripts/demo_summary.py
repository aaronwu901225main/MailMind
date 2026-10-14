"""用虛構郵件測試 Gemini API；不讀取你的實際 Gmail。"""
import argparse
from app.services.summarizer import summarize_with_gemini

SENDER = 'project@example.com'
SUBJECT = '10/19 專案討論與報告繳交通知'
BODY = (
    '大家好，本週一（2026/10/19）上午 10:00 將舉行線上專案討論。'
    '請大家在 2026/10/18 晚上 18:00 前提交測試報告與資料集統計。'
    '會議將討論模型評測結果，以及下一階段的資料標註計畫。'
)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--level', choices=['short', 'medium', 'full'], default='medium')
    args = parser.parse_args()
    print(f'摘要等級：{args.level}\n')
    print(summarize_with_gemini(SENDER, SUBJECT, BODY, args.level))


if __name__ == '__main__':
    main()
