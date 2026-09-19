-- Group multi-disc weddings and record each disc's video profile so the
-- restoration pipeline can deinterlace correctly per source. lsdvd reports
-- PAL/NTSC (stored in titles.video_json) but not field order; field_order here
-- is the idet-verified value from the disc registry (webapp/config/settings.py).
--   collection      wedding/event label; multi-disc sets share it (UI grouping)
--   video_standard  'pal' | 'ntsc'  -> drives scale/fps/colour matrix
--   field_order     'tff' | 'bff'   -> drives bwdif parity
ALTER TABLE discs ADD COLUMN collection TEXT;
ALTER TABLE discs ADD COLUMN video_standard TEXT;
ALTER TABLE discs ADD COLUMN field_order TEXT;

-- Backfill the already-restored Yacoob & Aysha discs (known-good values).
UPDATE discs SET collection='Yacoob And Aysha', video_standard='pal', field_order='bff'
 WHERE slug IN ('dvd1','dvd2');
