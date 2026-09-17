# Experimento: identificación de voz (wespeaker vs ECAPA)

Se ejecuta en orden con `.venv\Scripts\python.exe experimentos\voz\<paso>.py`:

1. `p1_diarizar.py` — pyannote en GPU sobre cada `grabaciones/*_mixed.wav` → `datos/voz/diar/`.
2. `p2_etiquetar.py` — nombre por turno desde el `.srt` (+ `meeting_speakers` de la BD) y energía de `mic.wav` → `datos/voz/segmentos.jsonl`.
3. `p3_embeddings.py` — fragmentos de 1–4 s, embeddings wespeaker (256 d) y ECAPA (192 d) → `datos/voz/fragmentos.jsonl` + `embeddings.npz`.
4. `p4_comparar.py` — EER e identificación **entre reuniones distintas** → `datos/voz/informe_comparacion.json`.
5. `p5_asignar.py <reunion>` — pone nombre a cada línea del `.srt`/`.txt` según `datos/voz/mapeo/<reunion>.json` (cluster → persona). Guarda el `.srt` previo como `*_mixed.sin_hablantes.srt`.
6. `p6_perfiles.py [--escribir]` — evalúa dejando una reunión fuera y regenera `datos/perfiles_voz.json`.
7. `p7_revisar.py <reunion>` — tabla de revisión (voz frente a transcripción, con frases y minutos) sin modificar nada.

Estado, objetivos y cómo añadir una reunión: `SESION_2026-09-17.md`.

`datos/voz/` es biométrico (RGPD) y no se versiona. El token de HuggingFace se toma de `HF_TOKEN` o de `~/.cache/huggingface/token`.

Etiquetas: "Javi" sale de `mic.wav` (no tiene fugas: ratio ~1 frente a ~0); el resto viene de la transcripción, que la asignó el LLM o los perfiles, así que es una etiqueta **débil**.

Normalización: L2 → restar la media de las medias por hablante → L2. Centrar con la media global es un error: el hablante dominante (Pedro, ~40 % de los fragmentos) pierde su propia voz (EER 12 % → 28 %).
