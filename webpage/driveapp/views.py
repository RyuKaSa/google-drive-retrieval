# driveapp/views.py

import os
import json
import io
import re

from django.shortcuts import render, redirect
from django.urls import reverse
from django.http import (
    JsonResponse,
    HttpResponseBadRequest,
    HttpResponseNotAllowed,
)
from django.views.decorators.csrf import csrf_exempt
from django.conf import settings

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ['https://www.googleapis.com/auth/drive.readonly']


def get_flow(request):
    redirect_uri = request.build_absolute_uri(
        reverse('driveapp:oauth2callback')
    )
    return Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [redirect_uri],
            }
        },
        scopes=SCOPES,
        redirect_uri=redirect_uri,
    )


def index(request):
    creds = request.session.get('credentials')
    return render(request, 'index.html', {
        'creds': creds,
        'api_key': os.getenv('GOOGLE_API_KEY'),
        'client_id': os.getenv('GOOGLE_CLIENT_ID'),
        'allowed_mimes': json.dumps(settings.DRIVE_ALLOWED_MIME_TYPES),
    })


def login(request):
    flow = get_flow(request)
    auth_url, _ = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent',
    )
    return redirect(auth_url)


def oauth2callback(request):
    try:
        flow = get_flow(request)
        flow.fetch_token(authorization_response=request.build_absolute_uri())
        creds = flow.credentials

        cfg = (flow.client_config.get('web')
               or flow.client_config.get('installed')
               or {})
        token_uri = cfg.get(
            'token_uri',
            'https://oauth2.googleapis.com/token'
        )

        request.session['credentials'] = {
            'token': creds.token,
            'refresh_token': creds.refresh_token,
            'token_uri': token_uri,
            'client_id': creds.client_id,
            'client_secret': creds.client_secret,
            'scopes': creds.scopes,
        }
        return redirect('driveapp:index')

    except Exception as e:
        return HttpResponseBadRequest(f"OAuth failed: {e}".encode('utf-8'))


def get_drive_service(request):
    creds_data = request.session.get('credentials')
    if not creds_data:
        return None

    creds = Credentials(**creds_data)
    try:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            request.session['credentials']['token'] = creds.token
        return build('drive', 'v3', credentials=creds)

    except RefreshError:
        del request.session['credentials']
        return None
    except Exception:
        return None


@csrf_exempt
def metadata(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])
    try:
        payload = request.body.decode('utf-8')
        docs    = json.loads(payload).get('docs', [])
    except (ValueError, AttributeError):
        return JsonResponse({'error': 'bad json'}, status=400)
    return JsonResponse(docs, safe=False)

@csrf_exempt
def fetch_and_download(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    # parse request
    try:
        payload = json.loads(request.body.decode('utf-8'))
        docs    = payload.get('docs', [])
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid JSON payload'.encode('utf-8'))

    # build Drive service
    service = get_drive_service(request)
    if not service:
        return HttpResponseBadRequest('Missing or expired Drive credentials'.encode('utf-8'))

    # prepare download directory
    allowed_mimes = set(settings.DRIVE_ALLOWED_MIME_TYPES)
    download_root = os.path.join(settings.BASE_DIR, 'downloads')
    os.makedirs(download_root, exist_ok=True)

    downloaded = []

    def recurse(items):
        for item in items:
            fid  = item.get('id')
            name = item.get('name')
            mime = item.get('mimeType')

            # 1) Native Google Docs -> export as PDF
            if mime == 'application/vnd.google-apps.document':
                request_media = service.files().export(
                    fileId=fid,
                    mimeType='application/pdf'
                )
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request_media)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                pdf_name = f"{name}.pdf"
                with open(os.path.join(download_root, pdf_name), 'wb') as out:
                    out.write(fh.getvalue())
                downloaded.append(pdf_name)

            # 2) Folder -> recurse (include shared drives)
            elif mime == 'application/vnd.google-apps.folder':
                page_token = None
                while True:
                    resp = service.files().list(
                        q=f"'{fid}' in parents and trashed=false",
                        fields='nextPageToken, files(id,name,mimeType)',
                        pageToken=page_token,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        corpora='allDrives'
                    ).execute()

                    recurse(resp.get('files', []))
                    page_token = resp.get('nextPageToken')
                    if not page_token:
                        break

            # 3) All other allowed types (including DOCX) -> binary download
            elif mime in allowed_mimes:
                request_media = service.files().get_media(fileId=fid)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request_media)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                with open(os.path.join(download_root, name), 'wb') as out:
                    out.write(fh.getvalue())
                downloaded.append(name)

            # unsupported MIME -> skip

    recurse(docs)
    return JsonResponse({'downloaded': downloaded}, status=200 if downloaded else 500)

@csrf_exempt
def search_drive(request):
    if request.method != 'POST':
        return HttpResponseNotAllowed(['POST'])

    try:
        payload       = json.loads(request.body.decode('utf-8'))
        raw_query     = payload.get('query', '').strip()
        selected_ids  = set(payload.get('selected_ids', []))
    except (ValueError, AttributeError):
        return HttpResponseBadRequest('Invalid JSON payload'.encode('utf-8'))

    words = [re.sub(r"'", r"\\'", w) for w in re.findall(r"\S+", raw_query)]
    if not words:
        return JsonResponse({'results': {}})

    # WE DECIDE SEARCH SCOPE HERE
    # Either names, or text content, or both
    name_q   = ' and '.join([f"name contains '{w}'"      for w in words])
    text_q   = ' and '.join([f"fullText contains '{w}'"  for w in words])

    service = get_drive_service(request)
    if not service:
        return HttpResponseBadRequest('Missing or expired Drive credentials'.encode('utf-8'))

    # SEARCH FUNCTION
    def run(q):
        resp = service.files().list(
            q=q,
            corpora='allDrives',
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id,name,mimeType)",
            pageSize=200
        ).execute()
        return resp.get('files', [])

    try:
        name_hits = run(name_q)
        text_hits = [f for f in run(text_q) if f['id'] not in {d['id'] for d in name_hits}]

        # Split into “selected” and “global”
        def split(hits):
            sel  = [h for h in hits if h['id'] in selected_ids]
            glob = [h for h in hits if h['id'] not in selected_ids]
            return sel, glob

        sel_name, glob_name = split(name_hits)
        sel_text, glob_text = split(text_hits)

        return JsonResponse({
            'selected': {
                'by_name'    : sel_name,
                'by_fulltext': sel_text
            },
            'global': {
                'by_name'    : glob_name,
                'by_fulltext': glob_text
            }
        })

    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)
