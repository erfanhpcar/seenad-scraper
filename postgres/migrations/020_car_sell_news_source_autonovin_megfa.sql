-- Allow autonovin and megfa as car_sell_news.source values.

ALTER TABLE car_sell_news DROP CONSTRAINT IF EXISTS ck_car_sell_news_source;
ALTER TABLE car_sell_news ADD CONSTRAINT ck_car_sell_news_source
    CHECK (source IN ('khodro45', 'car.ir', 'khodrobank', 'autonovin', 'megfa'));
