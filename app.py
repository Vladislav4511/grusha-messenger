from flask import Flask, render_template_string

app = Flask(__name__)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Мессенджер Груша</title>
    <style>
        body {
            background-color: #ffffff;
            color: #111111;
            margin: 0;
            padding: 20px;
            height: 100vh;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            justify-content: center;
            align-items: center;
            text-align: center;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        }
        h1 {
            font-size: clamp(2rem, 6vw, 4.5rem);
            font-weight: 700;
            margin: 0 0 24px 0;
            line-height: 1.2;
        }
        .roses {
            font-size: clamp(2.5rem, 7vw, 5rem);
            letter-spacing: 12px;
        }
    </style>
</head>
<body>
    <h1>Мессенджер груша был удалён.</h1>
    <div class="roses">🌹🌹🌹</div>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
