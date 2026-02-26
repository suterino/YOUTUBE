
I want a python script that reads the file follow.txt

with inside a list like this one:
https://www.youtube.com/@MarcoCasario
https://www.youtube.com/@DrVitoZennaro
https://www.youtube.com/@NateBJones

the python script will then create a file lastest_videos.html
with the list of all the videos of those youtubers that have been published in the last 10 days (put a variable inside the script, at the beginning that says how many days), make table that I can scroll, with the embedded youtube video, the title of the video, the link to the video, then one extra column with a radio button that I can click, if clicked it will download the transcript, for example if the video is:

Marco Casario - I Mercati seguono i REGIMI Economici e Il MEGLIO potrebbe essere Passato
https://www.youtube.com/watch?v=7DQkUFM6uvo&t=1689s
date: 2/16/2026
then download the transcript with:
yt-dlp --write-auto-sub --sub-lang it --skip-download -o MarcoCasario-02162026-IMercatiSeguonoIRegimiEconomici "https://www.youtube.com/watch?v=7DQkUFM6uvo&t=1689s"
note that the transcript is downloaded in the transcripts folder with the name: MarcoCasario-02162026-IMercatiSeguonoIRegimiEconomici.it.vtt
in the name of the file report the name of the youtuber, the date, the title of the video, make sure there are no spaces in the filename.

