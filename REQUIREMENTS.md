# JSE Stock Analysis Platform

## Overview

The **JSE Stock Analysis Platform** is an open-source, Python-based web application focused on providing investors with insights into companies listed on the Johannesburg Stock Exchange (JSE).

The application aggregates data from multiple free sources, performs automated web scraping, stores historical market information, and provides personalized investment dashboards, watchlists, and notifications.

The primary goal is to create an intelligent personal investment assistant capable of tracking favorite stocks, monitoring company news, identifying market trends, and eventually generating AI-powered investment insights.

---

# Objectives

* Monitor JSE listed companies
* Display real-time and historical market information
* Aggregate financial news from multiple sources
* Perform hourly data collection
* Build a personal watchlist
* Receive notifications when important events occur
* Visualize stock performance
* Perform technical analysis
* Calculate portfolio performance
* Support AI-powered stock summaries in future releases

---

# Technical Stack

## Frontend

* Python
* Streamlit
* Plotly
* AgGrid (streamlit-aggrid)

### Why

* Rapid development
* Interactive dashboards
* Excellent chart support
* Open source

---

## Backend

Python 3.13+

Application Structure

```
app/
    dashboard/
    services/
    collectors/
    scrapers/
    analytics/
    notifications/
    database/
    models/
```

---

## Database

SQLite (development)

Future

* PostgreSQL

ORM

* SQLAlchemy

Migration

* Alembic

---

## Scheduler

APScheduler

Runs background jobs

* Hourly stock updates
* Hourly news scraping
* Portfolio calculations
* Notification processing

---

## Caching

DiskCache

or

Redis (optional)

---

# Data Sources

The application combines multiple free data providers.

## Primary Market Data

### Twelve Data API

Purpose

* Daily prices
* Intraday prices
* Historical candles

Reason

* Good free tier
* REST API
* Reliable

---

## Secondary Market Data

Yahoo Finance

Using

```
yfinance
```

Purpose

* Backup data source
* Historical prices
* Dividends
* Splits
* Company metadata

---

## Financial Statements

Financial Modeling Prep

Used for

* Balance sheets
* Income statements
* Ratios

Free tier available.

---

## Foreign Exchange

ExchangeRate API

Used for

* USD/ZAR
* GBP/ZAR

---

# News Collection

The application aggregates news from multiple free sources.

## RSS

Collect RSS feeds from

* Moneyweb
* BusinessTech
* News24 Business
* Daily Investor

RSS is preferred over scraping whenever available.

---

## Web Scraping

When RSS is unavailable.

Libraries

```
BeautifulSoup4
Requests
Playwright
```

Scrape

* Headlines
* Summary
* Published date
* Article URL

Never scrape entire articles.

Respect robots.txt and website terms of use.

---

# Data Collection Pipeline

Every hour

```
Scheduler

↓

Fetch market prices

↓

Fetch company fundamentals

↓

Fetch dividends

↓

Collect news

↓

Run sentiment analysis

↓

Save database

↓

Generate notifications
```

---

# Favorite Stocks

Users can create a watchlist.

Example

```
NPN

CFR

AGL

SOL

SBK
```

Each favorite stock stores

* purchase price
* target price
* stop loss
* personal notes

---

# Notifications

Notifications are generated when

## Price

* Price increases more than X%
* Price decreases more than X%

---

## News

* New company article
* Breaking news

---

## Technical Indicators

* RSI overbought
* RSI oversold
* MACD crossover
* SMA crossover
* Volume spike

---

## Dividend

* Dividend announced
* Ex-dividend date
* Payment date

---

## Portfolio

* Daily gain
* Daily loss
* Weekly performance

---

# Notification Channels

Open source only.

Supported

* Email (SMTP)
* ntfy
* Apprise
* Desktop notifications
* Discord Webhooks
* Telegram Bot

Future

* Signal
* Matrix

---

# Dashboard

## Home

Displays

* JSE Index
* Portfolio Value
* Daily Gain
* Watchlist Summary
* Market News

---

## Watchlist

Displays

* Live price
* Daily change
* Weekly change
* Monthly change
* Volume
* Dividend yield

---

## Company View

Displays

* Company Overview
* Historical prices
* Financial statements
* Dividend history
* Insider news
* Technical indicators

---

## Charts

Using Plotly

Available charts

* Candlestick
* Line
* Moving averages
* RSI
* MACD
* Bollinger Bands
* Volume

---

# Technical Analysis

Libraries

```
pandas

numpy

ta

scipy
```

Indicators

* SMA
* EMA
* RSI
* MACD
* ATR
* Bollinger Bands
* OBV
* VWAP

---

# Portfolio Tracker

Users enter

* Quantity
* Purchase price
* Purchase date

Application calculates

* Total investment
* Current value
* Profit/Loss
* CAGR
* Dividend income

---

# Sentiment Analysis

Analyze collected news using

Transformers

or

FinBERT

Outputs

* Positive
* Neutral
* Negative

Future

Summarize news using an open-source LLM.

Examples

* Llama 3
* Mistral
* Qwen

Run locally using Ollama.

---

# AI Features (Future)

Stock Summary

Example

```
Naspers

Positive sentiment this week.

Revenue increased.

RSI indicates slight overbought conditions.

Recent news highlights continued investment in AI companies.
```

Portfolio Assistant

Examples

* Explain today's portfolio movement
* Summarize weekly performance
* Highlight unusual trading activity

---

# Project Structure

```
stock-analyzer/

├── app
│   ├── dashboard
│   ├── analytics
│   ├── collectors
│   ├── scrapers
│   ├── notifications
│   ├── database
│   ├── services
│   ├── models
│   ├── config
│   └── utils
│
├── tests
│
├── data
│
├── docker
│
├── requirements.txt
│
├── docker-compose.yml
│
└── README.md
```

---

# Open Source Libraries

| Purpose              | Library                     |
| -------------------- | --------------------------- |
| UI                   | Streamlit                   |
| Charts               | Plotly                      |
| Data                 | Pandas                      |
| Numerical            | NumPy                       |
| Technical Analysis   | ta                          |
| Database             | SQLAlchemy                  |
| Scheduler            | APScheduler                 |
| Web Scraping         | BeautifulSoup4              |
| Dynamic Scraping     | Playwright                  |
| HTTP                 | Requests                    |
| Notifications        | Apprise                     |
| Finance              | yfinance                    |
| Financial Statements | Financial Modeling Prep API |
| Market Data          | Twelve Data API             |
| Sentiment            | Transformers                |
| Local LLM            | Ollama                      |
| Testing              | Pytest                      |

---

# Future Roadmap

## Version 1

* Stock dashboard
* Watchlist
* Historical prices
* Hourly updates
* News aggregation

---

## Version 2

* Portfolio tracking
* Technical indicators
* Notifications
* Dividend tracking

---

## Version 3

* AI summaries
* Local LLM integration
* Sentiment analysis
* Strategy backtesting

---

## Version 4

* Stock screening
* Risk analysis
* Portfolio optimization
* Monte Carlo simulations
* Correlation matrix
* Sector comparison
* ETF tracking

---

# Guiding Principles

* 100% open-source technology stack
* API-first design with interchangeable data providers
* Modular architecture for future expansion
* Respect robots.txt and licensing when scraping
* Local-first development with optional cloud deployment
* Reproducible data collection and analytics
* Easy deployment using Docker
* Optimized for South African investors with a focus on the JSE
