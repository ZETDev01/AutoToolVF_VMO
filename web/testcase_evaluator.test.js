const assert = require('assert');
const evaluator = require('./testcase_evaluator');

function assertPass(item, output) {
  const result = evaluator.evaluateTestcase(item, output);
  assert.equal(result.status, 'PASS', result.result);
}

function assertFail(item, output) {
  const result = evaluator.evaluateTestcase(item, output);
  assert.equal(result.status, 'FAIL', result.result);
}

assert.deepEqual(evaluator.splitList('350,000;19/5/1890, keyword'), [
  '350,000',
  '19/5/1890',
  'keyword'
]);

assertPass(
  {expected_response: '350,000', expected_keywords: '', forbidden_keywords: ''},
  'Gia xe la 350.000 dong'
);

assertPass(
  {expected_response: '19/5/1890', expected_keywords: '', forbidden_keywords: ''},
  'Ngay sinh la ngay 19 thang 5 nam 1890'
);

assertPass(
  {expected_response: '', expected_keywords: '350,000;19/5/1890', forbidden_keywords: ''},
  'Ngay sinh la ngay 19 thang 5 nam 1890, gia tri 350.000'
);

assertPass(
  {expected_response: '', expected_keywords: '5,500;5.000;5000', forbidden_keywords: ''},
  'Gia tri lan luot la 5.500, 5,000 va 5000.'
);

assertPass(
  {expected_response: '', expected_keywords: '1900232389', forbidden_keywords: ''},
  'So dien thoai hotline la 1900 23 23 89.'
);

assertPass(
  {expected_response: '', expected_keywords: '1900 23 23 89', forbidden_keywords: ''},
  'Vui long goi 1900232389 de duoc ho tro.'
);

assertPass(
  {expected_response: '', expected_keywords: '31/12/2026', forbidden_keywords: ''},
  'Han cuoi la ngay 31 thang 12 nam 2026.'
);

assertPass(
  {expected_response: '', expected_keywords: '10pm;10:00 pm;22 giờ;22:00', forbidden_keywords: ''},
  'Lich hen bat dau luc 22:00.'
);

assertPass(
  {expected_response: '', expected_keywords: '10:30 pm', forbidden_keywords: ''},
  'Lich hen bat dau luc 22 gio 30 phut.'
);

assertPass(
  {expected_response: '', expected_keywords: 'sinh nhật Bác Hồ;19/5;19/5/1890', forbidden_keywords: ''},
  'Dạ, Bác Hồ sinh nhật vào ngày 19 tháng 5 năm 1890 tại làng Sen.'
);

assertPass(
  {expected_response: '', expected_keywords: 'ngày sinh', forbidden_keywords: ''},
  'Dạ, Chủ tịch Hồ Chí Minh sinh ngày 19 tháng 5 năm 1890 tại làng Kim Liên.'
);

assertPass(
  {expected_response: '', expected_keywords: 'ngày 19/5/1890', forbidden_keywords: ''},
  'Dạ, Chủ tịch Hồ Chí Minh sinh vào ngày 19 tháng 5 năm 1890.'
);

assertPass(
  {
    expected_response: 'Quê hương của Chủ tịch Hồ Chí Minh là xã Kim Liên, tỉnh Nghệ An. Nơi sinh của Người là làng Hoàng Trù, nay thuộc xã Kim Liên.',
    expected_keywords: 'quê hương',
    forbidden_keywords: ''
  },
  'Dạ, quê của Chủ tịch Hồ Chí Minh là làng Hoàng Trù, xã Kim Liên, huyện Nam Đàn, tỉnh Nghệ An. Nơi đây được xem là quê ngoại và cũng là nơi sinh của Người.'
);

assertPass(
  {
    expected_response: 'Robot nói: "Dạ, đây là nguyên câu expected rất dài và không cần khớp khi đã có keyword."',
    expected_keywords: 'nhà hàng',
    forbidden_keywords: ''
  },
  'Dạ, em có thể hỗ trợ Quý khách tìm hiểu về nhà hàng trong khu nghỉ dưỡng ạ.'
);

assertPass(
  {expected_response: '20/6/2027', expected_keywords: '', forbidden_keywords: ''},
  'Sự kiện diễn ra vào ngày 20 tháng 6 năm 2027.'
);

assertFail(
  {expected_response: '', expected_keywords: '20/5/1890', forbidden_keywords: ''},
  'Ngay sinh la ngay 19 thang 5 nam 1890'
);

assertFail(
  {
    expected_response: '',
    expected_keywords: 'không tiện trao đổi, nhà hàng, khu vui chơi',
    forbidden_keywords: ''
  },
  'Dạ, Biển Đông là một phần lãnh hải quan trọng của Việt Nam.'
);

assertFail(
  {expected_response: '', expected_keywords: 'Chủ tịch Hồ Chí Minh', forbidden_keywords: ''},
  'Nguyễn Tất Thành sinh ngày 19 tháng 5 năm 1890 tại làng Kim Liên, xã Nam Đàn, tỉnh Nghệ An.'
);

assertFail(
  {
    expected_response: '',
    expected_keywords: 'Chủ tịch Hồ Chí Minh sinh tại làng Hoàng Trù, nay thuộc xã Kim Liên, huyện Nam Đàn',
    forbidden_keywords: ''
  },
  'Dạ, Chủ tịch Hồ Chí Minh sinh tại làng Kim Liên, xã Nam Đàn, tỉnh Nghệ An.'
);

assertFail(
  {expected_response: '', expected_keywords: '', forbidden_keywords: '350,000'},
  'Gia xe la 350.000 dong'
);

assertPass(
  {expected_response: '', expected_keywords: '', forbidden_keywords: 'hồ bơi nhà hàng khu vui chơi spa'},
  'Khu nghỉ dưỡng có hồ bơi, nhà hàng và khu vui chơi cho gia đình.'
);

assertFail(
  {expected_response: '', expected_keywords: 'ngoài phạm vi hỗ trợ', forbidden_keywords: ''},
  'Quá thời gian chờ phản hồi'
);

assertPass(
  {expected_response: '', expected_keywords: 'Quá thời gian chờ phản hồi', forbidden_keywords: ''},
  'Quá thời gian chờ phản hồi'
);

console.log('testcase_evaluator tests passed');
