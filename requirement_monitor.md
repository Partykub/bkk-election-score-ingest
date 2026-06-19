ต้องการทำหน้าที่สามารถดูได้ว่าตอนนี้ข้อมูลแต่ละเขตเข้ามากี่ครั้ง ข้อมูลแต่ละเขตขาดข้อมูลอะไรบ้าง

และเราสามารถadd ข้อมูลได้ด้วย
หรือก็คือเป็นหน้า monitor ใช้จัดการเหตุการณ์ไม่คาดคิดทั้งหมดวางแผนหน่อยว่าจะมีอะไรบ้างแต่เท่าที่ผมรู้ก็คือเช่น json ด้านล่างก็จะมี title ที่อยู่ใน pageMeta ต้องสามารถกรอกเพื่อเปลี่ยนได้

{
    "schemaVersion": "1.0",
    "resource": "governor-results",
    "pageMeta": {
        "electionId": "bkk-governor-2026",
        "title": "ผลการเลือกตั้งผู้ว่าฯ กรุงเทพมหานคร",
        "resultStatus": "LIVE_COUNT",
        "generatedAt": "2026-06-16T03:12:25.334Z"
    },
    "summary": {
        "countedUnits": 10,
        "totalUnits": 50,
        "countedPercentage": 20.0,
        "eligibleVoters": null,
        "voterTurnout": null,
        "voterTurnoutPercentage": null,
        "validBallots": null,
        "invalidBallots": null,
        "abstainedBallots": null,
        "lastUpdatedAt": "2026-06-15T11:19:48Z"
    },
    "candidates": [
        {
            "candidateId": "pongsak",
            "candidateNumber": 3,
            "name": "พงษ์ศักดิ์ พัวพรพงษ์",
            "color": "#D57AF0",
            "voteCount": 1922,
            "votePercentage": 29.63,
            "rank": 1,
            "isLeading": true
        },
        {
            "candidateId": "prateep",
            "candidateNumber": 4,
            "name": "ประทีป วัชรโชคเกษม",
            "color": "#7148EF",
            "voteCount": 1874,
            "votePercentage": 28.89,
            "rank": 2,
            "isLeading": false
        },
        {
            "candidateId": "anucha",
            "candidateNumber": 5,
            "name": "อนุชา บูรพชัยศรี",
            "color": "#00A1E7",
            "voteCount": 1100,
            "votePercentage": 16.96,
            "rank": 3,
            "isLeading": false
        },
        {
            "candidateId": "samai",
            "candidateNumber": 2,
            "name": "สมัย ละเลิศ",
            "color": "#1AA7B1",
            "voteCount": 746,
            "votePercentage": 11.5,
            "rank": 4,
            "isLeading": false
        },
        {
            "candidateId": "korakasivat",
            "candidateNumber": 1,
            "name": "กรกสิวัฒน์ เกษมศรี",
            "color": "#F76198",
            "voteCount": 464,
            "votePercentage": 7.15,
            "rank": 5,
            "isLeading": false
        },
        {
            "candidateId": "phisan",
            "candidateNumber": 6,
            "name": "พิศาล กิตติเยาวมาลย์",
            "color": "#FF4646",
            "voteCount": 300,
            "votePercentage": 4.63,
            "rank": 6,
            "isLeading": false
        },
        {
            "candidateId": "chadchart",
            "candidateNumber": 9,
            "name": "ชัชชาติ สิทธิพันธุ์",
            "color": "#75B811",
            "voteCount": 60,
            "votePercentage": 0.93,
            "rank": 7,
            "isLeading": false
        },
        {
            "candidateId": "veerapoj",
            "candidateNumber": 8,
            "name": "วีรพจน์ ลือประสิทธิ์สกุล",
            "color": "#1764AE",
            "voteCount": 20,
            "votePercentage": 0.31,
            "rank": 8,
            "isLeading": false
        }
    ],
    "dataQuality": {
        "isComplete": false,
        "isDelayed": true,
        "warnings": [
            "eligibleVoters is unavailable or incomplete in approved results.",
            "voterTurnout is unavailable or incomplete in approved results.",
            "validBallots is unavailable or incomplete in approved results.",
            "invalidBallots is unavailable or incomplete in approved results.",
            "abstainedBallots is unavailable or incomplete in approved results."
        ]
    }
}